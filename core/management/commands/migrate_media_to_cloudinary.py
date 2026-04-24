from django.core.management.base import BaseCommand, CommandError

from core.media_storage import cloudinary_is_ready, should_upload_reference, sync_image_references_to_cloudinary
from core.models import Design, Fabric, TailorProfile


class Command(BaseCommand):
    help = 'Upload stored inline or Render-hosted image references to Cloudinary and save the CDN URLs.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--source-base-url',
            default='',
            help='Optional base URL used to resolve relative image paths, for example https://backend-13lk.onrender.com',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would change without saving updated URLs.',
        )

    def handle(self, *args, **options):
        if not cloudinary_is_ready():
            raise CommandError(
                'Cloudinary is not configured. Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET first.'
            )

        source_base_url = str(options.get('source_base_url') or '').strip()
        dry_run = bool(options.get('dry_run'))

        updated_rows = 0

        targets = [
            ('tailor profiles', TailorProfile.objects.all(), 'tailor-profiles', False),
            ('fabrics', Fabric.objects.all(), 'fabrics', True),
            ('designs', Design.objects.all(), 'designs', True),
        ]

        for label, queryset, folder, has_image_list in targets:
            self.stdout.write(f'Processing {label}...')

            for instance in queryset.iterator():
                current_image = str(getattr(instance, 'image', '') or '').strip()
                current_images = list(getattr(instance, 'images', []) or []) if has_image_list else []

                needs_primary_upload = should_upload_reference(
                    current_image,
                    migrate_remote=True,
                    source_base_url=source_base_url,
                )
                needs_list_upload = any(
                    should_upload_reference(image, migrate_remote=True, source_base_url=source_base_url)
                    for image in current_images
                )

                if not needs_primary_upload and not needs_list_upload:
                    continue

                new_image, new_images = sync_image_references_to_cloudinary(
                    current_image,
                    current_images,
                    folder=folder,
                    migrate_remote=True,
                    source_base_url=source_base_url,
                )

                update_fields = []
                if new_image != current_image:
                    instance.image = new_image
                    update_fields.append('image')
                if has_image_list and new_images != current_images:
                    instance.images = new_images
                    update_fields.append('images')

                if not update_fields:
                    continue

                updated_rows += 1
                if dry_run:
                    self.stdout.write(f'  DRY RUN: would update {label[:-1]} #{instance.pk}')
                    continue

                instance.save(update_fields=update_fields)
                self.stdout.write(self.style.SUCCESS(f'  Updated {label[:-1]} #{instance.pk}'))

        summary = f'Finished media migration. Updated {updated_rows} record(s).'
        if dry_run:
            summary = f'{summary} No database changes were written.'
        self.stdout.write(self.style.SUCCESS(summary))
