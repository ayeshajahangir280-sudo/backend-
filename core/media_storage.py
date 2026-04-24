import base64
import hashlib
import io
import re
import warnings
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from PIL import Image, ImageOps

try:
    import cloudinary
    import cloudinary.api
    import cloudinary.uploader
except ImportError:  # pragma: no cover - handled at runtime when dependency is unavailable.
    cloudinary = None


ABSOLUTE_URI_PATTERN = re.compile(r'^[a-z][a-z0-9+.-]*:', re.IGNORECASE)
INLINE_IMAGE_PATTERN = re.compile(r'^data:(image/[-+.\w]+);base64,(.+)$', re.IGNORECASE | re.DOTALL)
INLINE_IMAGE_EXTENSION_MAP = {
    'image/jpeg': 'jpg',
    'image/jpg': 'jpg',
    'image/png': 'png',
    'image/webp': 'webp',
    'image/gif': 'gif',
}
MAX_UPLOAD_IMAGE_BYTES = 15 * 1024 * 1024
MAX_UPLOAD_IMAGE_PIXELS = 12_000_000
MAX_UPLOAD_IMAGE_DIMENSION = 1600
UPLOAD_IMAGE_JPEG_QUALITY = 60


class MediaStorageError(Exception):
    """Raised when image storage could not be completed."""


def is_absolute_uri(value):
    return bool(ABSOLUTE_URI_PATTERN.match(str(value or '').strip()))


def is_cloudinary_url(value):
    return 'res.cloudinary.com/' in str(value or '').strip().lower()


def cloudinary_is_ready():
    return bool(
        cloudinary
        and getattr(settings, 'CLOUDINARY_CLOUD_NAME', '').strip()
        and getattr(settings, 'CLOUDINARY_API_KEY', '').strip()
        and getattr(settings, 'CLOUDINARY_API_SECRET', '').strip()
    )


@lru_cache(maxsize=1)
def configure_cloudinary():
    if not cloudinary_is_ready():
        return False

    cloudinary.config(
        cloud_name=settings.CLOUDINARY_CLOUD_NAME,
        api_key=settings.CLOUDINARY_API_KEY,
        api_secret=settings.CLOUDINARY_API_SECRET,
        secure=True,
    )
    return True


def build_cloudinary_folder(folder):
    base_folder = getattr(settings, 'CLOUDINARY_ROOT_FOLDER', 'fass-us').strip('/ ')
    current_folder = str(folder or '').strip('/ ')
    if base_folder and current_folder:
        return f'{base_folder}/{current_folder}'
    return current_folder or base_folder or 'fass-us'


def normalize_upload_source(value, *, source_base_url=''):
    normalized = str(value or '').strip()
    if not normalized:
        return ''
    if normalized.startswith('//'):
        return f'https:{normalized}'
    if normalized.startswith('/') and source_base_url:
        return f"{source_base_url.rstrip('/')}{normalized}"
    return normalized


def should_upload_reference(value, *, migrate_remote=False, source_base_url=''):
    normalized = normalize_upload_source(value, source_base_url=source_base_url)
    if not normalized or is_cloudinary_url(normalized):
        return False
    if normalized.lower().startswith('data:image/'):
        return True
    return migrate_remote and is_absolute_uri(normalized)


def build_cloudinary_public_id(source, *, folder):
    source_hash = hashlib.sha256(str(source).encode('utf-8')).hexdigest()
    return f"{build_cloudinary_folder(folder)}/{source_hash}"


def build_cloudinary_public_id_from_hash(source_hash, *, folder):
    return f"{build_cloudinary_folder(folder)}/{source_hash}"


def build_local_media_relative_path(source, *, folder, extension='jpg'):
    source_hash = hashlib.sha256(str(source).encode('utf-8')).hexdigest()
    return f"{build_cloudinary_folder(folder)}/{source_hash}.{extension}"


def build_local_media_relative_path_from_hash(source_hash, *, folder, extension='jpg'):
    return f"{build_cloudinary_folder(folder)}/{source_hash}.{extension}"


def build_local_media_url(relative_path):
    media_url = str(getattr(settings, 'MEDIA_URL', '/media/') or '/media/').rstrip('/')
    return f"{media_url}/{str(relative_path).lstrip('/')}"


def normalize_uploaded_image_bytes(raw_bytes):
    if not raw_bytes:
        raise MediaStorageError('Uploaded image is empty.')

    if len(raw_bytes) > MAX_UPLOAD_IMAGE_BYTES:
        raise MediaStorageError('Uploaded image is too large. Please choose a smaller image.')

    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw_bytes)) as image:
                width, height = image.size
                if width * height > MAX_UPLOAD_IMAGE_PIXELS:
                    raise MediaStorageError('Uploaded image dimensions are too large. Please resize the image and try again.')

                image = ImageOps.exif_transpose(image)
                if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
                    rgba_image = image.convert('RGBA')
                    background = Image.new('RGB', rgba_image.size, (255, 255, 255))
                    background.paste(rgba_image, mask=rgba_image.getchannel('A'))
                    image = background
                elif image.mode != 'RGB':
                    image = image.convert('RGB')

                image.thumbnail((MAX_UPLOAD_IMAGE_DIMENSION, MAX_UPLOAD_IMAGE_DIMENSION), Image.Resampling.LANCZOS)

                output = io.BytesIO()
                image.save(
                    output,
                    format='JPEG',
                    quality=UPLOAD_IMAGE_JPEG_QUALITY,
                    optimize=True,
                )
    except MediaStorageError:
        raise
    except Image.DecompressionBombWarning as exc:
        raise MediaStorageError('Uploaded image dimensions are too large. Please resize the image and try again.') from exc
    except Exception as exc:
        raise MediaStorageError('Uploaded file is not a valid image.') from exc

    return output.getvalue(), 'jpg'


def store_inline_image_locally(source, *, folder):
    match = INLINE_IMAGE_PATTERN.match(str(source or '').strip())
    if not match:
        raise MediaStorageError('Inline image data is invalid.')

    mime_type = match.group(1).strip().lower()
    extension = INLINE_IMAGE_EXTENSION_MAP.get(mime_type, 'jpg')
    relative_path = build_local_media_relative_path(source, folder=folder, extension=extension)
    target_path = Path(getattr(settings, 'MEDIA_ROOT')) / Path(relative_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if not target_path.exists():
        try:
            raw_bytes = base64.b64decode(match.group(2), validate=True)
        except Exception as exc:
            raise MediaStorageError('Inline image data could not be decoded.') from exc
        target_path.write_bytes(raw_bytes)

    return build_local_media_url(relative_path)


def store_uploaded_file(uploaded_file, *, folder):
    if not uploaded_file:
        return ''

    try:
        raw_bytes = uploaded_file.read()
        uploaded_file.seek(0)
    except Exception as exc:
        raise MediaStorageError('Uploaded image could not be read.') from exc

    normalized_bytes, extension = normalize_uploaded_image_bytes(raw_bytes)
    source_hash = hashlib.sha256(normalized_bytes).hexdigest()

    if cloudinary_is_ready():
        return upload_binary_to_cloudinary(
            normalized_bytes,
            folder=folder,
            source_hash=source_hash,
            filename=getattr(uploaded_file, 'name', f'{source_hash}.{extension}') or f'{source_hash}.{extension}',
        )

    relative_path = build_local_media_relative_path_from_hash(source_hash, folder=folder, extension=extension)
    target_path = Path(getattr(settings, 'MEDIA_ROOT')) / Path(relative_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if not target_path.exists():
        target_path.write_bytes(normalized_bytes)
    return build_local_media_url(relative_path)


def sync_uploaded_files_to_storage(primary_file, file_list=None, *, folder):
    normalized_files = []
    for candidate in file_list or []:
        if candidate:
            normalized_files.append(candidate)

    if primary_file and primary_file not in normalized_files:
        normalized_files = [primary_file, *normalized_files]

    stored_cache = {}

    def store_candidate(candidate):
        cache_key = id(candidate)
        if cache_key not in stored_cache:
            stored_cache[cache_key] = store_uploaded_file(candidate, folder=folder)
        return stored_cache[cache_key]

    synced_images = []
    for candidate in normalized_files:
        synced_value = store_candidate(candidate)
        if synced_value and synced_value not in synced_images:
            synced_images.append(synced_value)

    synced_primary = store_candidate(primary_file) if primary_file else ''
    if synced_primary and synced_primary not in synced_images:
        synced_images = [synced_primary, *synced_images]
    if synced_images and not synced_primary:
        synced_primary = synced_images[0]

    return synced_primary, synced_images


def upload_reference_to_cloudinary(source, *, folder):
    if not configure_cloudinary():
        raise MediaStorageError(
            'Cloudinary is not configured. Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET.'
        )

    public_id = build_cloudinary_public_id(source, folder=folder)

    try:
        result = cloudinary.uploader.upload(
            source,
            public_id=public_id,
            overwrite=False,
            unique_filename=False,
            use_filename=False,
            resource_type='image',
        )
    except Exception as exc:
        if 'already exists' in str(exc).lower():
            try:
                existing = cloudinary.api.resource(public_id, resource_type='image', type='upload')
            except Exception as resource_exc:  # pragma: no cover - network dependent.
                raise MediaStorageError(f'Cloudinary asset lookup failed: {resource_exc}') from resource_exc

            secure_url = existing.get('secure_url') or existing.get('url')
            if secure_url:
                return secure_url

        raise MediaStorageError(f'Cloudinary upload failed: {exc}') from exc

    secure_url = result.get('secure_url') or result.get('url')
    if not secure_url:
        raise MediaStorageError('Cloudinary upload succeeded but no public URL was returned.')
    return secure_url


def upload_binary_to_cloudinary(binary_data, *, folder, source_hash, filename='upload.jpg'):
    if not configure_cloudinary():
        raise MediaStorageError(
            'Cloudinary is not configured. Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET.'
        )

    public_id = build_cloudinary_public_id_from_hash(source_hash, folder=folder)
    upload_stream = io.BytesIO(binary_data)
    upload_stream.name = filename

    try:
        result = cloudinary.uploader.upload(
            upload_stream,
            public_id=public_id,
            overwrite=False,
            unique_filename=False,
            use_filename=False,
            resource_type='image',
        )
    except Exception as exc:
        if 'already exists' in str(exc).lower():
            try:
                existing = cloudinary.api.resource(public_id, resource_type='image', type='upload')
            except Exception as resource_exc:  # pragma: no cover - network dependent.
                raise MediaStorageError(f'Cloudinary asset lookup failed: {resource_exc}') from resource_exc

            secure_url = existing.get('secure_url') or existing.get('url')
            if secure_url:
                return secure_url

        raise MediaStorageError(f'Cloudinary upload failed: {exc}') from exc

    secure_url = result.get('secure_url') or result.get('url')
    if not secure_url:
        raise MediaStorageError('Cloudinary upload succeeded but no public URL was returned.')
    return secure_url


def sync_image_references_to_cloudinary(
    primary_image,
    image_list=None,
    *,
    folder,
    migrate_remote=False,
    source_base_url='',
    require_config=False,
):
    normalized_primary = str(primary_image or '').strip()
    normalized_images = []

    for candidate in image_list or []:
        normalized_candidate = str(candidate or '').strip()
        if normalized_candidate and normalized_candidate not in normalized_images:
            normalized_images.append(normalized_candidate)

    if normalized_primary and normalized_primary not in normalized_images:
        normalized_images = [normalized_primary, *normalized_images]
    if normalized_images and not normalized_primary:
        normalized_primary = normalized_images[0]

    uploaded_cache = {}

    def sync_value(value):
        normalized_value = normalize_upload_source(value, source_base_url=source_base_url)
        if not normalized_value:
            return ''
        if normalized_value in uploaded_cache:
            return uploaded_cache[normalized_value]
        if should_upload_reference(
            normalized_value,
            migrate_remote=migrate_remote,
            source_base_url=source_base_url,
        ):
            if not cloudinary_is_ready():
                if normalized_value.lower().startswith('data:image/'):
                    uploaded_cache[normalized_value] = store_inline_image_locally(normalized_value, folder=folder)
                    return uploaded_cache[normalized_value]
                if require_config:
                    raise MediaStorageError(
                        'Cloudinary is not configured. Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET.'
                    )
                uploaded_cache[normalized_value] = normalized_value
                return normalized_value
            uploaded_cache[normalized_value] = upload_reference_to_cloudinary(normalized_value, folder=folder)
            return uploaded_cache[normalized_value]
        uploaded_cache[normalized_value] = normalized_value
        return normalized_value

    synced_primary = sync_value(normalized_primary)
    synced_images = []

    for image in normalized_images:
        synced_value = sync_value(image)
        if synced_value and synced_value not in synced_images:
            synced_images.append(synced_value)

    if synced_primary and synced_primary not in synced_images:
        synced_images = [synced_primary, *synced_images]
    if synced_images and not synced_primary:
        synced_primary = synced_images[0]

    return synced_primary, synced_images
