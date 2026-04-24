# Django Backend

## Run locally

```bash
cd backend
python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

API base URL:

```txt
http://localhost:8000/api/
```

Database:

```txt
Loaded from backend/.env via DATABASE_URL
```

Cloudinary media storage:

```txt
CLOUDINARY_CLOUD_NAME=your-cloud-name
CLOUDINARY_API_KEY=your-api-key
CLOUDINARY_API_SECRET=your-api-secret
# Optional: changes the Cloudinary folder prefix, defaults to fass-us
CLOUDINARY_ROOT_FOLDER=fass-us
```

After adding the Cloudinary variables, backfill existing inline or Render-hosted image references:

```bash
python manage.py migrate_media_to_cloudinary --source-base-url https://backend-13lk.onrender.com
```

No demo users are included.
