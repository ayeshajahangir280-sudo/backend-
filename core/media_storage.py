import hashlib
import re
from functools import lru_cache

from django.conf import settings

try:
    import cloudinary
    import cloudinary.api
    import cloudinary.uploader
except ImportError:  # pragma: no cover - handled at runtime when dependency is unavailable.
    cloudinary = None


ABSOLUTE_URI_PATTERN = re.compile(r'^[a-z][a-z0-9+.-]*:', re.IGNORECASE)


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
