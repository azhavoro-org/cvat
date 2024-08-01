# Copyright (C) 2020-2022 Intel Corporation
# Copyright (C) 2022-2024 CVAT.ai Corporation
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import io
import math
from abc import ABCMeta, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from io import BytesIO
from typing import Any, Callable, Generic, Iterator, Optional, Tuple, Type, TypeVar, Union

import av
import cv2
import numpy as np
from PIL import Image
from rest_framework.exceptions import ValidationError

from cvat.apps.engine import models
from cvat.apps.engine.cache import DataWithMime, MediaCache
from cvat.apps.engine.media_extractors import (
    FrameQuality,
    IChunkWriter,
    IMediaReader,
    Mpeg4ChunkWriter,
    Mpeg4CompressedChunkWriter,
    RandomAccessIterator,
    VideoReader,
    ZipChunkWriter,
    ZipCompressedChunkWriter,
    ZipReader,
)
from cvat.apps.engine.mime_types import mimetypes

_T = TypeVar("_T")


class _ChunkLoader(metaclass=ABCMeta):
    def __init__(self, reader_class: IMediaReader) -> None:
        self.chunk_id: Optional[int] = None
        self.chunk_reader: Optional[RandomAccessIterator] = None
        self.reader_class = reader_class

    def load(self, chunk_id: int) -> RandomAccessIterator[Tuple[Any, str, int]]:
        if self.chunk_id != chunk_id:
            self.unload()

            self.chunk_id = chunk_id
            self.chunk_reader = RandomAccessIterator(
                self.reader_class([self.read_chunk(chunk_id)[0]])
            )
        return self.chunk_reader

    def unload(self):
        self.chunk_id = None
        if self.chunk_reader:
            self.chunk_reader.close()
            self.chunk_reader = None

    @abstractmethod
    def read_chunk(self, chunk_id: int) -> DataWithMime: ...


class _FileChunkLoader(_ChunkLoader):
    def __init__(
        self, reader_class: IMediaReader, get_chunk_path_callback: Callable[[int], str]
    ) -> None:
        super().__init__(reader_class)
        self.get_chunk_path = get_chunk_path_callback

    def read_chunk(self, chunk_id: int) -> DataWithMime:
        chunk_path = self.get_chunk_path(chunk_id)
        with open(chunk_path, "rb") as f:
            return (
                io.BytesIO(f.read()),
                mimetypes.guess_type(chunk_path)[0],
            )


class _BufferChunkLoader(_ChunkLoader):
    def __init__(
        self, reader_class: IMediaReader, get_chunk_callback: Callable[[int], DataWithMime]
    ) -> None:
        super().__init__(reader_class)
        self.get_chunk = get_chunk_callback

    def read_chunk(self, chunk_id: int) -> DataWithMime:
        return self.get_chunk(chunk_id)


class FrameOutputType(Enum):
    BUFFER = auto()
    PIL = auto()
    NUMPY_ARRAY = auto()


Frame2d = Union[BytesIO, np.ndarray, Image.Image]
Frame3d = BytesIO
AnyFrame = Union[Frame2d, Frame3d]


@dataclass
class DataWithMeta(Generic[_T]):
    data: _T
    mime: str
    checksum: int


class IFrameProvider(metaclass=ABCMeta):
    VIDEO_FRAME_EXT = ".PNG"
    VIDEO_FRAME_MIME = "image/png"

    def unload(self):
        pass

    @classmethod
    def _av_frame_to_png_bytes(cls, av_frame: av.VideoFrame) -> BytesIO:
        ext = cls.VIDEO_FRAME_EXT
        image = av_frame.to_ndarray(format="bgr24")
        success, result = cv2.imencode(ext, image)
        if not success:
            raise RuntimeError(f"Failed to encode image to '{ext}' format")
        return BytesIO(result.tobytes())

    def _convert_frame(
        self, frame: Any, reader_class: IMediaReader, out_type: FrameOutputType
    ) -> AnyFrame:
        if out_type == FrameOutputType.BUFFER:
            return self._av_frame_to_png_bytes(frame) if reader_class is VideoReader else frame
        elif out_type == FrameOutputType.PIL:
            return frame.to_image() if reader_class is VideoReader else Image.open(frame)
        elif out_type == FrameOutputType.NUMPY_ARRAY:
            if reader_class is VideoReader:
                image = frame.to_ndarray(format="bgr24")
            else:
                image = np.array(Image.open(frame))
                if len(image.shape) == 3 and image.shape[2] in {3, 4}:
                    image[:, :, :3] = image[:, :, 2::-1]  # RGB to BGR
            return image
        else:
            raise RuntimeError("unsupported output type")

    @abstractmethod
    def validate_frame_number(self, frame_number: int) -> int: ...

    @abstractmethod
    def validate_chunk_number(self, chunk_number: int) -> int: ...

    @abstractmethod
    def get_chunk_number(self, frame_number: int) -> int: ...

    @abstractmethod
    def get_preview(self) -> DataWithMeta[BytesIO]: ...

    @abstractmethod
    def get_chunk(
        self, chunk_number: int, *, quality: FrameQuality = FrameQuality.ORIGINAL
    ) -> DataWithMeta[BytesIO]: ...

    @abstractmethod
    def get_frame(
        self,
        frame_number: int,
        *,
        quality: FrameQuality = FrameQuality.ORIGINAL,
        out_type: FrameOutputType = FrameOutputType.BUFFER,
    ) -> DataWithMeta[AnyFrame]: ...

    @abstractmethod
    def get_frame_context_images(
        self,
        frame_number: int,
    ) -> Optional[DataWithMeta[BytesIO]]: ...

    @abstractmethod
    def iterate_frames(
        self,
        *,
        start_frame: Optional[int] = None,
        stop_frame: Optional[int] = None,
        quality: FrameQuality = FrameQuality.ORIGINAL,
        out_type: FrameOutputType = FrameOutputType.BUFFER,
    ) -> Iterator[DataWithMeta[AnyFrame]]: ...


class TaskFrameProvider(IFrameProvider):
    def __init__(self, db_task: models.Task) -> None:
        self._db_task = db_task

    def validate_frame_number(self, frame_number: int) -> int:
        start = self._db_task.data.start_frame
        stop = self._db_task.data.stop_frame
        if frame_number not in range(start, stop + 1, self._db_task.data.get_frame_step()):
            raise ValidationError(
                f"Invalid frame '{frame_number}'. "
                f"The frame number should be in the [{start}, {stop}] range"
            )

        return frame_number

    def validate_chunk_number(self, chunk_number: int) -> int:
        start_chunk = 0
        stop_chunk = math.ceil(self._db_task.data.size / self._db_task.data.chunk_size)
        if not (start_chunk <= chunk_number <= stop_chunk):
            raise ValidationError(
                f"Invalid chunk number '{chunk_number}'. "
                f"The chunk number should be in the [{start_chunk}, {stop_chunk}] range"
            )

        return chunk_number

    def get_chunk_number(self, frame_number: int) -> int:
        return int(frame_number) // self._db_task.data.chunk_size

    def get_preview(self) -> DataWithMeta[BytesIO]:
        return self._get_segment_frame_provider(self._db_task.data.start_frame).get_preview()

    def get_chunk(
        self, chunk_number: int, *, quality: FrameQuality = FrameQuality.ORIGINAL
    ) -> DataWithMeta[BytesIO]:
        return_type = DataWithMeta[BytesIO]
        chunk_number = self.validate_chunk_number(chunk_number)

        db_data = self._db_task.data
        step = db_data.get_frame_step()
        task_chunk_start_frame = chunk_number * db_data.chunk_size
        task_chunk_stop_frame = (chunk_number + 1) * db_data.chunk_size - 1
        task_chunk_frame_set = set(
            range(
                db_data.start_frame + task_chunk_start_frame * step,
                min(db_data.start_frame + task_chunk_stop_frame * step, db_data.stop_frame) + step,
                step,
            )
        )

        matching_segments = sorted(
            [
                s
                for s in self._db_task.segment_set.all()
                if s.type == models.SegmentType.RANGE
                if not task_chunk_frame_set.isdisjoint(s.frame_set)
            ],
            key=lambda s: s.start_frame,
        )
        assert matching_segments

        if len(matching_segments) == 1:
            segment_frame_provider = SegmentFrameProvider(matching_segments[0])
            return segment_frame_provider.get_chunk(
                segment_frame_provider.get_chunk_number(task_chunk_start_frame), quality=quality
            )

        # Create and return a joined chunk
        # TODO: refactor into another class, optimize (don't visit frames twice)
        task_chunk_frames = []
        for db_segment in matching_segments:
            segment_frame_provider = SegmentFrameProvider(db_segment)
            segment_frame_set = db_segment.frame_set

            for task_chunk_frame_id in task_chunk_frame_set:
                if task_chunk_frame_id not in segment_frame_set:
                    continue

                frame = segment_frame_provider.get_frame(
                    task_chunk_frame_id, quality=quality, out_type=FrameOutputType.BUFFER
                ).data
                task_chunk_frames.append((frame, None, None))

        writer_classes: dict[FrameQuality, Type[IChunkWriter]] = {
            FrameQuality.COMPRESSED: (
                Mpeg4CompressedChunkWriter
                if db_data.compressed_chunk_type == models.DataChoice.VIDEO
                else ZipCompressedChunkWriter
            ),
            FrameQuality.ORIGINAL: (
                Mpeg4ChunkWriter
                if db_data.original_chunk_type == models.DataChoice.VIDEO
                else ZipChunkWriter
            ),
        }

        image_quality = (
            100
            if writer_classes[quality] in [Mpeg4ChunkWriter, ZipChunkWriter]
            else db_data.image_quality
        )
        mime_type = (
            "video/mp4"
            if writer_classes[quality] in [Mpeg4ChunkWriter, Mpeg4CompressedChunkWriter]
            else "application/zip"
        )

        kwargs = {}
        if self._db_task.dimension == models.DimensionType.DIM_3D:
            kwargs["dimension"] = models.DimensionType.DIM_3D
        merged_chunk_writer = writer_classes[quality](image_quality, **kwargs)

        buffer = io.BytesIO()
        merged_chunk_writer.save_as_chunk(
            task_chunk_frames,
            buffer,
            compress_frames=False,
            zip_compress_level=1,
        )
        buffer.seek(0)

        # TODO: add caching

        return return_type(data=buffer, mime=mime_type, checksum=None)

    def get_frame(
        self,
        frame_number: int,
        *,
        quality: FrameQuality = FrameQuality.ORIGINAL,
        out_type: FrameOutputType = FrameOutputType.BUFFER,
    ) -> DataWithMeta[AnyFrame]:
        return self._get_segment_frame_provider(frame_number).get_frame(
            frame_number, quality=quality, out_type=out_type
        )

    def get_frame_context_images(
        self,
        frame_number: int,
    ) -> Optional[DataWithMeta[BytesIO]]:
        return self._get_segment_frame_provider(frame_number).get_frame_context_images(frame_number)

    def iterate_frames(
        self,
        *,
        start_frame: Optional[int] = None,
        stop_frame: Optional[int] = None,
        quality: FrameQuality = FrameQuality.ORIGINAL,
        out_type: FrameOutputType = FrameOutputType.BUFFER,
    ) -> Iterator[DataWithMeta[AnyFrame]]:
        # TODO: optimize segment access
        for idx in range(start_frame, (stop_frame + 1) if stop_frame else None):
            yield self.get_frame(idx, quality=quality, out_type=out_type)

    def _get_segment(self, validated_frame_number: int) -> models.Segment:
        return next(
            s
            for s in self._db_task.segment_set.all()
            if s.type == models.SegmentType.RANGE
            if validated_frame_number in s.frame_set
        )

    def _get_segment_frame_provider(self, frame_number: int) -> SegmentFrameProvider:
        return SegmentFrameProvider(self._get_segment(self.validate_frame_number(frame_number)))


class SegmentFrameProvider(IFrameProvider):
    def __init__(self, db_segment: models.Segment) -> None:
        super().__init__()
        self._db_segment = db_segment

        db_data = db_segment.task.data

        reader_class: dict[models.DataChoice, IMediaReader] = {
            models.DataChoice.IMAGESET: ZipReader,
            models.DataChoice.VIDEO: VideoReader,
        }

        self._loaders: dict[FrameQuality, _ChunkLoader] = {}
        if db_data.storage_method == models.StorageMethodChoice.CACHE:
            cache = MediaCache()

            self._loaders[FrameQuality.COMPRESSED] = _BufferChunkLoader(
                reader_class=reader_class[db_data.compressed_chunk_type],
                get_chunk_callback=lambda chunk_idx: cache.get_segment_chunk(
                    db_segment, chunk_idx, quality=FrameQuality.COMPRESSED
                ),
            )

            self._loaders[FrameQuality.ORIGINAL] = _BufferChunkLoader(
                reader_class=reader_class[db_data.original_chunk_type],
                get_chunk_callback=lambda chunk_idx: cache.get_segment_chunk(
                    db_segment, chunk_idx, quality=FrameQuality.ORIGINAL
                ),
            )
        else:
            self._loaders[FrameQuality.COMPRESSED] = _FileChunkLoader(
                reader_class=reader_class[db_data.compressed_chunk_type],
                get_chunk_path_callback=lambda chunk_idx: db_data.get_compressed_segment_chunk_path(
                    chunk_idx, segment=db_segment.id
                ),
            )

            self._loaders[FrameQuality.ORIGINAL] = _FileChunkLoader(
                reader_class=reader_class[db_data.original_chunk_type],
                get_chunk_path_callback=lambda chunk_idx: db_data.get_original_segment_chunk_path(
                    chunk_idx, segment=db_segment.id
                ),
            )

    def unload(self):
        for loader in self._loaders.values():
            loader.unload()

    def __len__(self):
        return self._db_segment.frame_count

    def validate_frame_number(self, frame_number: int) -> Tuple[int, int, int]:
        frame_sequence = list(self._db_segment.frame_set)
        if frame_number not in frame_sequence:
            raise ValidationError(f"Incorrect requested frame number: {frame_number}")

        # TODO: maybe optimize search
        chunk_number, frame_position = divmod(
            frame_sequence.index(frame_number), self._db_segment.task.data.chunk_size
        )
        return frame_number, chunk_number, frame_position

    def get_chunk_number(self, frame_number: int) -> int:
        return int(frame_number) // self._db_segment.task.data.chunk_size

    def validate_chunk_number(self, chunk_number: int) -> int:
        segment_size = self._db_segment.frame_count
        start_chunk = 0
        stop_chunk = math.ceil(segment_size / self._db_segment.task.data.chunk_size)
        if not (start_chunk <= chunk_number <= stop_chunk):
            raise ValidationError(
                f"Invalid chunk number '{chunk_number}'. "
                f"The chunk number should be in the [{start_chunk}, {stop_chunk}] range"
            )

        return chunk_number

    def get_preview(self) -> DataWithMeta[BytesIO]:
        cache = MediaCache()
        preview, mime = cache.get_or_set_segment_preview(self._db_segment)
        return DataWithMeta[BytesIO](preview, mime=mime, checksum=None)

    def get_chunk(
        self, chunk_number: int, *, quality: FrameQuality = FrameQuality.ORIGINAL
    ) -> DataWithMeta[BytesIO]:
        chunk_number = self.validate_chunk_number(chunk_number)
        chunk_data, mime = self._loaders[quality].read_chunk(chunk_number)
        return DataWithMeta[BytesIO](chunk_data, mime=mime, checksum=None)

    def get_frame(
        self,
        frame_number: int,
        *,
        quality: FrameQuality = FrameQuality.ORIGINAL,
        out_type: FrameOutputType = FrameOutputType.BUFFER,
    ) -> DataWithMeta[AnyFrame]:
        return_type = DataWithMeta[AnyFrame]

        _, chunk_number, frame_offset = self.validate_frame_number(frame_number)
        loader = self._loaders[quality]
        chunk_reader = loader.load(chunk_number)
        frame, frame_name, _ = chunk_reader[frame_offset]

        frame = self._convert_frame(frame, loader.reader_class, out_type)
        if loader.reader_class is VideoReader:
            return return_type(frame, mime=self.VIDEO_FRAME_MIME, checksum=None)

        return return_type(frame, mime=mimetypes.guess_type(frame_name)[0], checksum=None)

    def get_frame_context_images(
        self,
        frame_number: int,
    ) -> Optional[DataWithMeta[BytesIO]]:
        # TODO: refactor, optimize
        cache = MediaCache()

        if self._db_segment.task.data.storage_method == models.StorageMethodChoice.CACHE:
            data, mime = cache.get_frame_context_images(self._db_segment.task.data, frame_number)
        else:
            data, mime = cache.prepare_context_images(self._db_segment.task.data, frame_number)

        if not data:
            return None

        return DataWithMeta[BytesIO](data, mime=mime, checksum=None)

    def iterate_frames(
        self,
        *,
        start_frame: Optional[int] = None,
        stop_frame: Optional[int] = None,
        quality: FrameQuality = FrameQuality.ORIGINAL,
        out_type: FrameOutputType = FrameOutputType.BUFFER,
    ) -> Iterator[DataWithMeta[AnyFrame]]:
        for idx in range(start_frame, (stop_frame + 1) if stop_frame else None):
            yield self.get_frame(idx, quality=quality, out_type=out_type)


class JobFrameProvider(SegmentFrameProvider):
    def __init__(self, db_job: models.Job) -> None:
        super().__init__(db_job.segment)


def make_frame_provider(data_source: Union[models.Job, models.Task, Any]) -> IFrameProvider:
    if isinstance(data_source, models.Task):
        frame_provider = TaskFrameProvider(data_source)
    elif isinstance(data_source, models.Job):
        frame_provider = JobFrameProvider(data_source)
    else:
        raise TypeError(f"Unexpected data source type {type(data_source)}")

    return frame_provider
