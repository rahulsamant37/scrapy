"""
Images Pipeline

See documentation in topics/media-pipeline.rst
"""

from __future__ import annotations

import functools
import hashlib
import warnings
from contextlib import suppress
from io import BytesIO
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    Type,
    Union,
    cast,
)

from itemadapter import ItemAdapter

from scrapy.exceptions import DropItem, NotConfigured, ScrapyDeprecationWarning
from scrapy.http import Request, Response
from scrapy.http.request import NO_CALLBACK
from scrapy.pipelines.files import (
    FileException,
    FilesPipeline,
    FTPFilesStore,
    GCSFilesStore,
    S3FilesStore,
    _md5sum,
)
from scrapy.settings import Settings
from scrapy.utils.python import get_func_args, to_bytes

if TYPE_CHECKING:
    from os import PathLike

    from PIL import Image

    # typing.Self requires Python 3.11
    from typing_extensions import Self

    from scrapy import Spider
    from scrapy.pipelines.media import FileInfoOrError, MediaPipeline


class NoimagesDrop(DropItem):
    """Product with no images exception"""

    def __init__(self, *args: Any, **kwargs: Any):
        warnings.warn(
            "The NoimagesDrop class is deprecated",
            category=ScrapyDeprecationWarning,
            stacklevel=2,
        )
        super().__init__(*args, **kwargs)


class ImageException(FileException):
    """General image error exception"""


class ImagesPipeline(FilesPipeline):
    """Abstract pipeline that implement the image thumbnail generation logic"""

    MEDIA_NAME: str = "image"

    # Uppercase attributes kept for backward compatibility with code that subclasses
    # ImagesPipeline. They may be overridden by settings.
    MIN_WIDTH: int = 0
    MIN_HEIGHT: int = 0
    EXPIRES: int = 90
    THUMBS: Dict[str, Tuple[int, int]] = {}
    DEFAULT_IMAGES_URLS_FIELD = "image_urls"
    DEFAULT_IMAGES_RESULT_FIELD = "images"

    def __init__(
        self,
        store_uri: Union[str, PathLike[str]],
        download_func: Optional[Callable[[Request, Spider], Response]] = None,
        settings: Union[Settings, Dict[str, Any], None] = None,
    ):
        try:
            from PIL import Image

            self._Image = Image
        except ImportError:
            raise NotConfigured(
                "ImagesPipeline requires installing Pillow 4.0.0 or later"
            )

        super().__init__(store_uri, settings=settings, download_func=download_func)

        if isinstance(settings, dict) or settings is None:
            settings = Settings(settings)

        resolve = functools.partial(
            self._key_for_pipe,
            base_class_name="ImagesPipeline",
            settings=settings,
        )
        self.expires: int = settings.getint(resolve("IMAGES_EXPIRES"), self.EXPIRES)

        if not hasattr(self, "IMAGES_RESULT_FIELD"):
            self.IMAGES_RESULT_FIELD: str = self.DEFAULT_IMAGES_RESULT_FIELD
        if not hasattr(self, "IMAGES_URLS_FIELD"):
            self.IMAGES_URLS_FIELD: str = self.DEFAULT_IMAGES_URLS_FIELD

        self.images_urls_field: str = settings.get(
            resolve("IMAGES_URLS_FIELD"), self.IMAGES_URLS_FIELD
        )
        self.images_result_field: str = settings.get(
            resolve("IMAGES_RESULT_FIELD"), self.IMAGES_RESULT_FIELD
        )
        self.min_width: int = settings.getint(
            resolve("IMAGES_MIN_WIDTH"), self.MIN_WIDTH
        )
        self.min_height: int = settings.getint(
            resolve("IMAGES_MIN_HEIGHT"), self.MIN_HEIGHT
        )
        self.thumbs: Dict[str, Tuple[int, int]] = settings.get(
            resolve("IMAGES_THUMBS"), self.THUMBS
        )

        self._deprecated_convert_image: Optional[bool] = None

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        s3store: Type[S3FilesStore] = cast(Type[S3FilesStore], cls.STORE_SCHEMES["s3"])
        s3store.AWS_ACCESS_KEY_ID = settings["AWS_ACCESS_KEY_ID"]
        s3store.AWS_SECRET_ACCESS_KEY = settings["AWS_SECRET_ACCESS_KEY"]
        s3store.AWS_SESSION_TOKEN = settings["AWS_SESSION_TOKEN"]
        s3store.AWS_ENDPOINT_URL = settings["AWS_ENDPOINT_URL"]
        s3store.AWS_REGION_NAME = settings["AWS_REGION_NAME"]
        s3store.AWS_USE_SSL = settings["AWS_USE_SSL"]
        s3store.AWS_VERIFY = settings["AWS_VERIFY"]
        s3store.POLICY = settings["IMAGES_STORE_S3_ACL"]

        gcs_store: Type[GCSFilesStore] = cast(
            Type[GCSFilesStore], cls.STORE_SCHEMES["gs"]
        )
        gcs_store.GCS_PROJECT_ID = settings["GCS_PROJECT_ID"]
        gcs_store.POLICY = settings["IMAGES_STORE_GCS_ACL"] or None

        ftp_store: Type[FTPFilesStore] = cast(
            Type[FTPFilesStore], cls.STORE_SCHEMES["ftp"]
        )
        ftp_store.FTP_USERNAME = settings["FTP_USER"]
        ftp_store.FTP_PASSWORD = settings["FTP_PASSWORD"]
        ftp_store.USE_ACTIVE_MODE = settings.getbool("FEED_STORAGE_FTP_ACTIVE")

        store_uri = settings["IMAGES_STORE"]
        return cls(store_uri, settings=settings)

    def file_downloaded(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> str:
        return self.image_downloaded(response, request, info, item=item)

    def image_downloaded(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> str:
        checksum: Optional[str] = None
        for path, image, buf in self.get_images(response, request, info, item=item):
            if checksum is None:
                buf.seek(0)
                checksum = _md5sum(buf)
            width, height = image.size
            self.store.persist_file(
                path,
                buf,
                info,
                meta={"width": width, "height": height},
                headers={"Content-Type": "image/jpeg"},
            )
        assert checksum is not None
        return checksum

    def get_images(
        self,
        response: Response,
        request: Request,
        info: MediaPipeline.SpiderInfo,
        *,
        item: Any = None,
    ) -> Iterable[Tuple[str, Image.Image, BytesIO]]:
        path = self.file_path(request, response=response, info=info, item=item)
        orig_image = self._Image.open(BytesIO(response.body))

        width, height = orig_image.size
        if width < self.min_width or height < self.min_height:
            raise ImageException(
                "Image too small "
                f"({width}x{height} < "
                f"{self.min_width}x{self.min_height})"
            )

        if self._deprecated_convert_image is None:
            self._deprecated_convert_image = "response_body" not in get_func_args(
                self.convert_image
            )
            if self._deprecated_convert_image:
                warnings.warn(
                    f"{self.__class__.__name__}.convert_image() method overridden in a deprecated way, "
                    "overridden method does not accept response_body argument.",
                    category=ScrapyDeprecationWarning,
                )

        if self._deprecated_convert_image:
            image, buf = self.convert_image(orig_image)
        else:
            image, buf = self.convert_image(
                orig_image, response_body=BytesIO(response.body)
            )
        yield path, image, buf

        for thumb_id, size in self.thumbs.items():
            thumb_path = self.thumb_path(
                request, thumb_id, response=response, info=info, item=item
            )
            if self._deprecated_convert_image:
                thumb_image, thumb_buf = self.convert_image(image, size)
            else:
                thumb_image, thumb_buf = self.convert_image(image, size, buf)
            yield thumb_path, thumb_image, thumb_buf

    def convert_image(
        self,
        image: Image.Image,
        size: Optional[Tuple[int, int]] = None,
        response_body: Optional[BytesIO] = None,
    ) -> Tuple[Image.Image, BytesIO]:
        if response_body is None:
            warnings.warn(
                f"{self.__class__.__name__}.convert_image() method called in a deprecated way, "
                "method called without response_body argument.",
                category=ScrapyDeprecationWarning,
                stacklevel=2,
            )

        if image.format in ("PNG", "WEBP") and image.mode == "RGBA":
            background = self._Image.new("RGBA", image.size, (255, 255, 255))
            background.paste(image, image)
            image = background.convert("RGB")
        elif image.mode == "P":
            image = image.convert("RGBA")
            background = self._Image.new("RGBA", image.size, (255, 255, 255))
            background.paste(image, image)
            image = background.convert("RGB")
        elif image.mode != "RGB":
            image = image.convert("RGB")

        if size:
            image = image.copy()
            try:
                # Image.Resampling.LANCZOS was added in Pillow 9.1.0
                # remove this try except block,
                # when updating the minimum requirements for Pillow.
                resampling_filter = self._Image.Resampling.LANCZOS
            except AttributeError:
                resampling_filter = self._Image.ANTIALIAS  # type: ignore[attr-defined]
            image.thumbnail(size, resampling_filter)
        elif response_body is not None and image.format == "JPEG":
            return image, response_body

        buf = BytesIO()
        image.save(buf, "JPEG")
        return image, buf

    def get_media_requests(
        self, item: Any, info: MediaPipeline.SpiderInfo
    ) -> List[Request]:
        urls = ItemAdapter(item).get(self.images_urls_field, [])
        return [Request(u, callback=NO_CALLBACK) for u in urls]

    def item_completed(
        self, results: List[FileInfoOrError], item: Any, info: MediaPipeline.SpiderInfo
    ) -> Any:
        with suppress(KeyError):
            ItemAdapter(item)[self.images_result_field] = [x for ok, x in results if ok]
        return item

    def file_path(
        self,
        request: Request,
        response: Optional[Response] = None,
        info: Optional[MediaPipeline.SpiderInfo] = None,
        *,
        item: Any = None,
    ) -> str:
        image_guid = hashlib.sha1(to_bytes(request.url)).hexdigest()  # nosec
        return f"full/{image_guid}.jpg"

    def thumb_path(
        self,
        request: Request,
        thumb_id: str,
        response: Optional[Response] = None,
        info: Optional[MediaPipeline.SpiderInfo] = None,
        *,
        item: Any = None,
    ) -> str:
        thumb_guid = hashlib.sha1(to_bytes(request.url)).hexdigest()  # nosec
        return f"thumbs/{thumb_id}/{thumb_guid}.jpg"
