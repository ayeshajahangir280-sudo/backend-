import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def env_csv(name: str, default=None):
    raw_value = os.getenv(name, '')
    values = [item.strip() for item in raw_value.split(',') if item.strip()]
    if values:
        return values
    return list(default or [])


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(BASE_DIR / '.env')

SECRET_KEY = os.getenv('SECRET_KEY', 'django-insecure-lj$&=)6@*3s+u%m8!&1@=bfm5g--ig4+2=u!g%v^p@l(t46ms7')
DEBUG = env_flag('DEBUG', False)
RENDER_EXTERNAL_HOSTNAME = os.getenv('RENDER_EXTERNAL_HOSTNAME', '').strip()
ALLOWED_HOSTS = env_csv('ALLOWED_HOSTS', default=['127.0.0.1', 'localhost'])
if RENDER_EXTERNAL_HOSTNAME and RENDER_EXTERNAL_HOSTNAME not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)
MAX_API_REQUEST_BODY_SIZE = int(os.getenv('MAX_API_REQUEST_BODY_SIZE', 8 * 1024 * 1024))
MAX_API_FORM_FIELDS = int(os.getenv('MAX_API_FORM_FIELDS', 200))
DATABASE_CONN_MAX_AGE = int(os.getenv('DATABASE_CONN_MAX_AGE', '600'))
DATABASE_SSL_REQUIRE = env_flag('DATABASE_SSL_REQUIRE', True)

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'corsheaders',
    'rest_framework',
    'rest_framework.authtoken',
    'core',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.gzip.GZipMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'core.middleware.RequestSizeLimitMiddleware',
    'core.middleware.PublicCorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'backend.wsgi.application'

DATABASE_URL = os.getenv('DATABASE_URL')

if not DATABASE_URL:
    raise RuntimeError('DATABASE_URL is not set. Add it to backend/.env or your environment.')

DATABASES = {
    'default': dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=DATABASE_CONN_MAX_AGE,
        ssl_require=DATABASE_SSL_REQUIRE,
    )
}
DATABASES['default']['CONN_HEALTH_CHECKS'] = True

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Karachi'

USE_I18N = True
USE_TZ = True
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'fass-us-api-cache',
        'TIMEOUT': 120,
    }
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
AUTH_USER_MODEL = 'core.User'
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_API_REQUEST_BODY_SIZE
FILE_UPLOAD_MAX_MEMORY_SIZE = MAX_API_REQUEST_BODY_SIZE
DATA_UPLOAD_MAX_NUMBER_FIELDS = MAX_API_FORM_FIELDS

CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = False
CORS_ALLOW_HEADERS = ['*']
CORS_ALLOW_METHODS = ['DELETE', 'GET', 'OPTIONS', 'PATCH', 'POST', 'PUT']
CSRF_TRUSTED_ORIGINS = env_csv('CSRF_TRUSTED_ORIGINS')

CLOUDINARY_CLOUD_NAME = os.getenv('CLOUDINARY_CLOUD_NAME', '').strip()
CLOUDINARY_API_KEY = os.getenv('CLOUDINARY_API_KEY', '').strip()
CLOUDINARY_API_SECRET = os.getenv('CLOUDINARY_API_SECRET', '').strip()
CLOUDINARY_ROOT_FOLDER = os.getenv('CLOUDINARY_ROOT_FOLDER', 'fass-us').strip() or 'fass-us'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework.authentication.TokenAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.AllowAny',
    ),
    'EXCEPTION_HANDLER': 'core.exceptions.api_exception_handler',
}
