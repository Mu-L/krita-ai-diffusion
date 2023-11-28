from __future__ import annotations
import asyncio
from collections import deque
from copy import copy
from datetime import datetime
from enum import Enum, Flag
from typing import Deque, NamedTuple, Optional, cast
from PyQt5.QtCore import Qt, QObject, QUuid, pyqtSignal

from . import eventloop, workflow, NetworkError, settings, util
from .image import Image, ImageCollection, Mask, Bounds
from .client import ClientMessage, ClientEvent, filter_supported_styles, resolve_sd_version
from .document import Document, LayerObserver
from .pose import Pose
from .style import Style, Styles
from .workflow import Control, ControlMode, Conditioning, LiveParams
from .connection import Connection, ConnectionState
from .properties import Property, PropertyMeta
import krita


class State(Flag):
    queued = 0
    executing = 1
    finished = 2
    cancelled = 3


class JobKind(Enum):
    diffusion = 0
    control_layer = 1
    upscaling = 2
    live_preview = 3


class Job:
    id: str | None
    kind: JobKind
    state = State.queued
    prompt: str
    bounds: Bounds
    control: ControlLayer | None = None
    timestamp: datetime
    _results: ImageCollection

    def __init__(self, id: str | None, kind: JobKind, prompt: str, bounds: Bounds):
        self.id = id
        self.kind = kind
        self.prompt = prompt
        self.bounds = bounds
        self.timestamp = datetime.now()
        self._results = ImageCollection()

    @property
    def results(self):
        return self._results


class JobQueue(QObject):
    """Queue of waiting, ongoing and finished jobs for one document."""

    class Item(NamedTuple):
        job: str
        image: int

    count_changed = pyqtSignal()
    selection_changed = pyqtSignal()
    job_finished = pyqtSignal(Job)

    _entries: Deque[Job]
    _selection: Item | None = None
    _memory_usage = 0  # in MB

    def __init__(self):
        super().__init__()
        self._entries = deque()

    def add(self, id: str, prompt: str, bounds: Bounds):
        self._add(Job(id, JobKind.diffusion, prompt, bounds))

    def add_control(self, control: ControlLayer, bounds: Bounds):
        job = Job(None, JobKind.control_layer, f"[Control] {control.mode.text}", bounds)
        job.control = control
        return self._add(job)

    def add_upscale(self, bounds: Bounds):
        job = Job(None, JobKind.upscaling, f"[Upscale] {bounds.width}x{bounds.height}", bounds)
        return self._add(job)

    def add_live(self, prompt: str, bounds: Bounds):
        job = Job(None, JobKind.live_preview, prompt, bounds)
        return self._add(job)

    def _add(self, job: Job):
        self._entries.append(job)
        self.count_changed.emit()
        return job

    def remove(self, job: Job):
        # Diffusion jobs: kept for history, pruned according to meomry usage
        # Control layer jobs: removed immediately once finished
        self._entries.remove(job)
        self.count_changed.emit()

    def find(self, id: str):
        return next((j for j in self._entries if j.id == id), None)

    def count(self, state: State):
        return sum(1 for j in self._entries if j.state is state)

    def set_results(self, job: Job, results: ImageCollection):
        job._results = results
        if job.kind is JobKind.diffusion:
            self._memory_usage += results.size / (1024**2)
            self.prune(keep=job)

    def notify_started(self, job: Job):
        job.state = State.executing
        self.count_changed.emit()

    def notify_finished(self, job: Job):
        job.state = State.finished
        self.job_finished.emit(job)
        self.count_changed.emit()

    def prune(self, keep: Job):
        while self._memory_usage > settings.history_size and self._entries[0] != keep:
            discarded = self._entries.popleft()
            self._memory_usage -= discarded._results.size / (1024**2)

    def select(self, job_id: str, index: int):
        self.selection = self.Item(job_id, index)

    def any_executing(self):
        return any(j.state is State.executing for j in self._entries)

    def __len__(self):
        return len(self._entries)

    def __getitem__(self, i):
        return self._entries[i]

    def __iter__(self):
        return iter(self._entries)

    @property
    def selection(self):
        return self._selection

    @selection.setter
    def selection(self, value: Item | None):
        self._selection = value
        self.selection_changed.emit()

    @property
    def memory_usage(self):
        return self._memory_usage


class Workspace(Enum):
    generation = 0
    upscaling = 1
    live = 2


class ControlLayer(QObject, metaclass=PropertyMeta):
    mode = Property(ControlMode.image)
    layer_id = Property(QUuid())
    strength = Property(1.0)
    end = Property(1.0)
    is_supported = Property(True)
    is_pose_vector = Property(False)
    can_generate = Property(True)
    has_active_job = Property(False)
    show_end = Property(False)
    error_text = Property("")

    _model: Model
    _generate_job: Job | None = None

    def __init__(self, model: Model, mode: ControlMode, layer_id: QUuid):
        from . import root

        super().__init__()
        self._model = model
        self.mode = mode
        self.layer_id = layer_id
        self._update_is_supported()
        self._update_is_pose_vector()

        self.mode_changed.connect(self._update_is_supported)
        model.style_changed.connect(self._update_is_supported)
        root.connection.state_changed.connect(self._update_is_supported)
        self.mode_changed.connect(self._update_is_pose_vector)
        self.layer_id_changed.connect(self._update_is_pose_vector)
        model.jobs.job_finished.connect(self._update_active_job)
        settings.changed.connect(self._handle_settings)

    @property
    def layer(self):
        layer = self._model.image_layers.find(self.layer_id)
        assert layer is not None, "Control layer has been deleted"
        return layer

    def get_image(self, bounds: Optional[Bounds] = None):
        layer = self.layer
        if self.mode is ControlMode.image and not layer.bounds().isEmpty():
            bounds = None  # ignore mask bounds, use layer bounds
        image = self._model.document.get_layer_image(layer, bounds)
        if self.mode.is_lines or self.mode is ControlMode.stencil:
            image.make_opaque(background=Qt.GlobalColor.white)
        return Control(self.mode, image, self.strength, self.end)

    def generate(self):
        self._generate_job = self._model.generate_control_layer(self)
        self.has_active_job = True

    def _update_is_supported(self):
        from . import root

        is_supported = True
        if client := root.connection.client_if_connected:
            sdver = resolve_sd_version(self._model.style, client)
            if self.mode is ControlMode.image:
                if client.ip_adapter_model[sdver] is None:
                    self.error_text = f"The server is missing the IP-Adapter model"
                    is_supported = False
            elif client.control_model[self.mode][sdver] is None:
                filenames = self.mode.filenames(sdver)
                if filenames:
                    self.error_text = f"The ControlNet model is not installed {filenames}"
                else:
                    self.error_text = f"Not supported for {sdver.value}"
                is_supported = False

        self.is_supported = is_supported
        self.show_end = self.is_supported and settings.show_control_end
        self.can_generate = is_supported and self.mode not in [
            ControlMode.image,
            ControlMode.stencil,
        ]

    def _update_is_pose_vector(self):
        self.is_pose_vector = self.mode is ControlMode.pose and self.layer.type() == "vectorlayer"

    def _update_active_job(self):
        active = self._generate_job is not None and self._generate_job.state is not State.finished
        if self.has_active_job and not active:
            self._job = None  # job done
        self.has_active_job = active

    def _handle_settings(self, name: str, value: object):
        if name == "show_control_end":
            self.show_end = self.is_supported and settings.show_control_end


class ControlLayerList(QObject):
    """List of control layers for one document."""

    added = pyqtSignal(ControlLayer)
    removed = pyqtSignal(ControlLayer)

    _model: Model
    _layers: list[ControlLayer]
    _last_mode = ControlMode.scribble

    def __init__(self, model: Model):
        super().__init__()
        self._model = model
        self._layers = []
        model.image_layers.changed.connect(self._update_layer_list)

    def add(self):
        layer = self._model.document.active_layer.uniqueId()
        control = ControlLayer(self._model, self._last_mode, layer)
        control.mode_changed.connect(self._update_last_mode)
        self._layers.append(control)
        self.added.emit(control)

    def remove(self, control: ControlLayer):
        self._layers.remove(control)
        self.removed.emit(control)

    def _update_last_mode(self, mode: ControlMode):
        self._last_mode = mode

    def _update_layer_list(self):
        # Remove layers that have been deleted
        layer_ids = [l.uniqueId() for l in self._model.image_layers]
        to_remove = [l for l in self._layers if l.layer_id not in layer_ids]
        for l in to_remove:
            self.remove(l)

    def __len__(self):
        return len(self._layers)

    def __getitem__(self, i):
        return self._layers[i]

    def __iter__(self):
        return iter(self._layers)


class Model(QObject, metaclass=PropertyMeta):
    """Represents diffusion workflows for a specific Krita document. Stores all inputs related to
    image generation. Launches generation jobs. Listens to server messages and keeps a
    list of finished, currently running and enqueued jobs.
    """

    _doc: Document
    _connection: Connection
    _layer: Optional[krita.Node] = None
    _live_result: Optional[Image] = None
    _image_layers: LayerObserver

    has_error_changed = pyqtSignal(bool)

    workspace = Property(Workspace.generation, setter="set_workspace")
    style = Property(Styles.list().default)
    prompt = Property("")
    negative_prompt = Property("")
    control: ControlLayerList
    strength = Property(1.0)
    upscale: UpscaleParams
    live: LiveParams
    progress = Property(0.0)
    jobs: JobQueue
    error = Property("")
    can_apply_result = Property(False)

    task: Optional[asyncio.Task] = None

    def __init__(self, document: Document, connection: Connection):
        super().__init__()
        self._doc = document
        self._image_layers = document.create_layer_observer()
        self._connection = connection
        self.control = ControlLayerList(self)
        self.upscale = UpscaleParams(self)
        self.live = LiveParams()
        self.jobs = JobQueue()

        self.jobs.job_finished.connect(self.update_preview)
        self.jobs.selection_changed.connect(self.update_preview)
        self.error_changed.connect(lambda: self.has_error_changed.emit(self.has_error))

        if client := connection.client_if_connected:
            self.style = next(iter(filter_supported_styles(Styles.list(), client)), self.style)
            self.upscale.upscaler = client.default_upscaler

    def generate(self):
        """Enqueue image generation for the current setup."""
        ok, msg = self._doc.check_color_mode()
        if not ok and msg:
            self.report_error(msg)
            return

        image = None
        extent = self._doc.extent

        mask, selection_bounds = self._doc.create_mask_from_selection(
            grow=settings.selection_grow / 100,
            feather=settings.selection_feather / 100,
            padding=settings.selection_padding / 100,
        )
        image_bounds = workflow.compute_bounds(extent, mask.bounds if mask else None, self.strength)
        if mask is not None or self.strength < 1.0:
            image = self._get_current_image(image_bounds)
        if selection_bounds is not None:
            selection_bounds = Bounds.apply_crop(selection_bounds, image_bounds)
            selection_bounds = Bounds.minimum_size(selection_bounds, 64, image_bounds.extent)

        control = [c.get_image(image_bounds) for c in self.control]
        conditioning = Conditioning(self.prompt, self.negative_prompt, control)
        conditioning.area = selection_bounds if self.strength == 1.0 else None

        self.clear_error()
        self.task = eventloop.run(
            _report_errors(self, self._generate(image_bounds, conditioning, image, mask))
        )

    async def _generate(
        self,
        bounds: Bounds,
        conditioning: Conditioning,
        image: Optional[Image],
        mask: Optional[Mask],
    ):
        client = self._connection.client
        style, strength = self.style, self.strength
        if not self.jobs.any_executing():
            self.progress = 0.0

        if mask is not None:
            mask_bounds_rel = Bounds(  # mask bounds relative to cropped image
                mask.bounds.x - bounds.x, mask.bounds.y - bounds.y, *mask.bounds.extent
            )
            bounds = mask.bounds  # absolute mask bounds, required to insert result image
            mask.bounds = mask_bounds_rel

        if image is None and mask is None:
            assert strength == 1
            job = workflow.generate(client, style, bounds.extent, conditioning)
        elif mask is None and strength < 1:
            assert image is not None
            job = workflow.refine(client, style, image, conditioning, strength)
        elif strength == 1:
            assert image is not None and mask is not None
            job = workflow.inpaint(client, style, image, mask, conditioning)
        else:
            assert image is not None and mask is not None and strength < 1
            job = workflow.refine_region(client, style, image, mask, conditioning, strength)

        job_id = await client.enqueue(job)
        self.jobs.add(job_id, conditioning.prompt, bounds)

    def upscale_image(self):
        image = self._doc.get_image(Bounds(0, 0, *self._doc.extent))
        job = self.jobs.add_upscale(Bounds(0, 0, *self.upscale.target_extent))
        self.clear_error()
        self.task = eventloop.run(
            _report_errors(self, self._upscale_image(job, image, copy(self.upscale)))
        )

    async def _upscale_image(self, job: Job, image: Image, params: UpscaleParams):
        client = self._connection.client
        if params.upscaler == "":
            params.upscaler = client.default_upscaler
        if params.use_diffusion:
            work = workflow.upscale_tiled(
                client, image, params.upscaler, params.factor, self.style, params.strength
            )
        else:
            work = workflow.upscale_simple(client, image, params.upscaler, params.factor)
        job.id = await client.enqueue(work)
        self._doc.resize(params.target_extent)

    def generate_live(self):
        bounds = Bounds(0, 0, *self._doc.extent)
        image = None
        if self.live.strength < 1:
            image = self._get_current_image(bounds)
        control = [c.get_image(bounds) for c in self.control]
        cond = Conditioning(self.prompt, self.negative_prompt, control)
        job = self.jobs.add_live(self.prompt, bounds)
        self.clear_error()
        self.task = eventloop.run(
            _report_errors(self, self._generate_live(job, image, self.style, cond))
        )

    async def _generate_live(self, job: Job, image: Image | None, style: Style, cond: Conditioning):
        client = self._connection.client
        if image:
            work = workflow.refine(client, style, image, cond, self.live.strength, self.live)
        else:
            work = workflow.generate(client, style, self._doc.extent, cond, self.live)
        job.id = await client.enqueue(work)

    def _get_current_image(self, bounds: Bounds):
        exclude = [  # exclude control layers from projection
            cast(krita.Node, c.image)
            for c in self.control
            if c.mode not in [ControlMode.image, ControlMode.blur]
        ]
        if self._layer:  # exclude preview layer
            exclude.append(self._layer)
        return self._doc.get_image(bounds, exclude_layers=exclude)

    def generate_control_layer(self, control: ControlLayer):
        ok, msg = self._doc.check_color_mode()
        if not ok and msg:
            self.report_error(msg)
            return

        image = self._doc.get_image(Bounds(0, 0, *self._doc.extent))
        job = self.jobs.add_control(control, Bounds(0, 0, *image.extent))
        self.clear_error()
        self.task = eventloop.run(
            _report_errors(self, self._generate_control_layer(job, image, control.mode))
        )
        return job

    async def _generate_control_layer(self, job: Job, image: Image, mode: ControlMode):
        client = self._connection.client
        work = workflow.create_control_image(image, mode)
        job.id = await client.enqueue(work)

    def cancel(self, active=False, queued=False):
        if queued:
            to_remove = [job for job in self.jobs if job.state is State.queued]
            if len(to_remove) > 0:
                self._connection.clear_queue()
                for job in to_remove:
                    self.jobs.remove(job)
        if active and self.jobs.any_executing():
            self._connection.interrupt()

    def report_progress(self, value):
        self.progress = value

    def report_error(self, message: str):
        self.error = message
        self.live.is_active = False

    def clear_error(self):
        if self.error != "":
            self.error = ""

    def handle_message(self, message: ClientMessage):
        job = self.jobs.find(message.job_id)
        if job is None:
            util.client_logger.error(f"Received message {message} for unknown job.")
            return

        if message.event is ClientEvent.progress:
            self.jobs.notify_started(job)
            self.report_progress(message.progress)
        elif message.event is ClientEvent.finished:
            if message.images:
                self.jobs.set_results(job, message.images)
            if job.kind is JobKind.control_layer:
                assert job.control is not None
                job.control.layer_id = self.add_control_layer(job, message.result).uniqueId()
            elif job.kind is JobKind.upscaling:
                self.add_upscale_layer(job)
            elif job.kind is JobKind.live_preview and len(job.results) > 0:
                self._live_result = job.results[0]
            self.progress = 1
            self.jobs.notify_finished(job)
            if job.kind is not JobKind.diffusion:
                self.jobs.remove(job)
            elif job.kind is JobKind.diffusion and self._layer is None and job.id:
                self.jobs.select(job.id, 0)
        elif message.event is ClientEvent.interrupted:
            job.state = State.cancelled
            self.report_progress(0)
        elif message.event is ClientEvent.error:
            job.state = State.cancelled
            self.report_error(f"Server execution error: {message.error}")

    def update_preview(self):
        if selection := self.jobs.selection:
            self.show_preview(selection.job, selection.image)
            self.can_apply_result = True
        else:
            self.hide_preview()
            self.can_apply_result = False

    def show_preview(self, job_id: str, index: int, name_prefix="Preview"):
        job = self.jobs.find(job_id)
        assert job is not None, "Cannot show preview, invalid job id"
        name = f"[{name_prefix}] {job.prompt}"
        if self._layer and self._layer.parentNode() is None:
            self._layer = None
        if self._layer is not None:
            self._layer.setName(name)
            self._doc.set_layer_content(self._layer, job.results[index], job.bounds)
        else:
            self._layer = self._doc.insert_layer(name, job.results[index], job.bounds)
            self._layer.setLocked(True)

    def hide_preview(self):
        if self._layer is not None:
            self._doc.hide_layer(self._layer)

    def apply_current_result(self):
        """Promote the preview layer to a user layer."""
        assert self._layer and self.can_apply_result
        self._layer.setLocked(False)
        self._layer.setName(self._layer.name().replace("[Preview]", "[Generated]"))
        self._layer = None

    def add_control_layer(self, job: Job, result: Optional[dict]):
        assert job.kind is JobKind.control_layer and job.control
        if job.control.mode is ControlMode.pose and result is not None:
            pose = Pose.from_open_pose_json(result)
            pose.scale(job.bounds.extent)
            return self._doc.insert_vector_layer(job.prompt, pose.to_svg(), below=self._layer)
        elif len(job.results) > 0:
            return self._doc.insert_layer(job.prompt, job.results[0], job.bounds, below=self._layer)
        return self.document.active_layer  # Execution was cached and no image was produced

    def add_upscale_layer(self, job: Job):
        assert job.kind is JobKind.upscaling
        assert len(job.results) > 0, "Upscaling job did not produce an image"
        if self._layer:
            self._layer.remove()
            self._layer = None
        self._doc.insert_layer(job.prompt, job.results[0], job.bounds)

    def add_live_layer(self):
        assert self._live_result is not None
        self._doc.insert_layer(
            f"[Live] {self.prompt}", self._live_result, Bounds(0, 0, *self._doc.extent)
        )

    def set_workspace(self, workspace: Workspace):
        if self.workspace is Workspace.live:
            self.live.is_active = False
        self._workspace = workspace
        self.workspace_changed.emit(workspace)

    @property
    def history(self):
        return (job for job in self.jobs if job.state is State.finished)

    @property
    def has_live_result(self):
        return self._live_result is not None

    @property
    def has_error(self):
        return self.error != ""

    @property
    def document(self):
        return self._doc

    @property
    def image_layers(self):
        return self._image_layers

    @property
    def is_active(self):
        return self._doc.is_active

    @property
    def is_valid(self):
        return self._doc.is_valid


class UpscaleParams:
    upscaler = ""
    factor = 2.0
    use_diffusion = True
    strength = 0.3

    _model: Model

    def __init__(self, model: Model):
        self._model = model
        # if client := Connection.instance().client_if_connected:
        #     self.upscaler = client.default_upscaler
        # else:
        #     self.upscaler = ""

    @property
    def target_extent(self):
        return self._model.document.extent * self.factor


async def _report_errors(parent, coro):
    try:
        return await coro
    except NetworkError as e:
        parent.report_error(f"{util.log_error(e)} [url={e.url}, code={e.code}]")
    except Exception as e:
        parent.report_error(util.log_error(e))
