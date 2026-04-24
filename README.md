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

Render Postgres setup:

```txt
1. Use the internal Render Postgres URL for the live backend service.
2. Use the external Render Postgres URL locally in backend/.env.
3. Keep sslmode=require on local/external connections.
4. This repo now includes backend/.env.example and a root render.yaml Blueprint.
```

If you want Render to create and wire the backend service and database from this repo, use the root [render.yaml](</d:/Downloads/app/render.yaml>).

If you already created the backend service or database manually in Render, keep those and just update their environment variables instead of creating a second Blueprint-managed copy.

Local setup after switching from Neon:

```bash
cd backend
copy .env.example .env
# Then replace DATABASE_URL with your Render EXTERNAL database URL
python manage.py migrate
python manage.py runserver
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
