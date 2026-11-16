"""Windows camera and location capture using the current user's permissions."""

from asyncio import get_running_loop, wait_for
from contextlib import closing, contextmanager
from datetime import timedelta, timezone
from threading import current_thread, main_thread

from capture_core import Location


class CaptureUnavailable(RuntimeError):
    """Windows did not supply a usable capture."""


@contextmanager
def windows_capture_apartment():
    """Open COM on the main thread around the entire asyncio.run call."""
    if current_thread() is not main_thread():
        raise CaptureUnavailable("请从主线程调用 Windows 采集，以便系统处理权限。")

    try:
        get_running_loop()
    except RuntimeError:
        pass
    else:
        raise CaptureUnavailable("请在启动异步采集之前初始化 Windows 组件。")

    from winrt.runtime import ApartmentType, init_apartment, uninit_apartment

    try:
        init_apartment(ApartmentType.SINGLE_THREADED)
    except OSError as error:
        raise CaptureUnavailable(
            f"Windows 采集组件初始化失败{_error_code(error)}。"
        ) from None
    try:
        yield
    finally:
        uninit_apartment()


def _error_code(error: OSError) -> str:
    code = getattr(error, "winerror", None)
    if code is None and error.args:
        code = error.args[0]
    return f"（0x{code & 0xFFFFFFFF:08X}）" if isinstance(code, int) else ""


async def capture_photo() -> bytes:
    """Capture a new JPEG from the Windows default video input."""
    try:
        return await _read_camera()
    except ImportError:
        raise CaptureUnavailable("缺少 Python/WinRT 摄像头组件。") from None
    except PermissionError:
        raise CaptureUnavailable("Windows 未允许访问摄像头。") from None
    except TimeoutError:
        raise CaptureUnavailable("Windows 摄像头初始化或拍照超时。") from None
    except OSError as error:
        raise CaptureUnavailable(
            f"Windows 摄像头初始化或拍照失败{_error_code(error)}。"
        ) from None


async def _read_camera() -> bytes:
    from winrt.windows.devices.enumeration import (
        DeviceAccessInformation,
        DeviceAccessStatus,
        DeviceClass,
    )
    from winrt.windows.media.capture import (
        MediaCapture,
        MediaCaptureInitializationSettings,
        StreamingCaptureMode,
    )
    from winrt.windows.media.mediaproperties import ImageEncodingProperties
    from winrt.windows.storage.streams import DataReader, InMemoryRandomAccessStream

    access = DeviceAccessInformation.create_from_device_class(DeviceClass.VIDEO_CAPTURE)
    if access.current_status != DeviceAccessStatus.ALLOWED:
        raise CaptureUnavailable("Windows 未允许访问摄像头。")

    settings = MediaCaptureInitializationSettings()
    settings.streaming_capture_mode = StreamingCaptureMode.VIDEO

    with closing(MediaCapture()) as camera:
        await wait_for(camera.initialize_async(settings), timeout=8)
        with closing(InMemoryRandomAccessStream()) as stream:
            await wait_for(
                camera.capture_photo_to_stream_async(
                    ImageEncodingProperties.create_jpeg(), stream
                ),
                timeout=8,
            )
            size = stream.size
            if not 4 <= size <= 0xFFFFFFFF:
                raise CaptureUnavailable("摄像头返回了无效的照片长度。")

            with closing(DataReader(stream.get_input_stream_at(0))) as reader:
                if await wait_for(reader.load_async(size), timeout=8) != size:
                    raise CaptureUnavailable("摄像头返回的照片数据不完整。")
                data = bytearray(size)
                reader.read_bytes(data)

    if not data.startswith(b"\xff\xd8\xff") or not data.endswith(b"\xff\xd9"):
        raise CaptureUnavailable("摄像头未返回完整的 JPEG。")
    return bytes(data)


async def capture_location() -> Location:
    """Request a fresh Windows location fix without choosing a fallback source."""
    try:
        return await wait_for(_read_location(), timeout=17)
    except ImportError:
        raise CaptureUnavailable("缺少 Python/WinRT 定位组件。") from None
    except PermissionError:
        raise CaptureUnavailable("Windows 未允许访问位置服务。") from None
    except TimeoutError:
        raise CaptureUnavailable("Windows 获取当前位置超时。") from None
    except OSError as error:
        raise CaptureUnavailable(
            f"Windows 未取得当前位置{_error_code(error)}。"
        ) from None


async def _read_location() -> Location:
    from winrt.windows.devices.enumeration import (
        DeviceAccessInformation,
        DeviceAccessStatus,
        DeviceClass,
    )
    from winrt.windows.devices.geolocation import (
        Geolocator,
        PositionAccuracy,
        PositionSource,
    )

    access = DeviceAccessInformation.create_from_device_class(DeviceClass.LOCATION)
    if access.current_status != DeviceAccessStatus.ALLOWED:
        raise CaptureUnavailable("Windows 未允许访问位置服务。")

    locator = Geolocator()
    locator.desired_accuracy = PositionAccuracy.HIGH
    position = await locator.get_geoposition_async(timedelta(0), timedelta(seconds=15))
    coordinate = position.coordinate
    point = coordinate.point
    if point.spatial_reference_id != 4326:
        raise CaptureUnavailable("Windows 返回了无法识别的坐标制式。")

    observed_at = coordinate.position_source_timestamp or coordinate.timestamp
    if observed_at is None or observed_at.utcoffset() is None:
        raise CaptureUnavailable("Windows 未提供有效的定位时间。")

    source = coordinate.position_source
    source_name = "wifi" if source == PositionSource.WI_FI else source.name.lower()
    return Location(
        latitude=point.position.latitude,
        longitude=point.position.longitude,
        accuracy_m=coordinate.accuracy,
        observed_at=observed_at.astimezone(timezone.utc),
        source=source_name,
        coordinate_system="WGS84",
    )
