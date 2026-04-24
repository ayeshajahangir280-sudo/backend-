import base64
import io

from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase
from django.test import override_settings

from .models import Design, Fabric, MeasurementProfile, Order, TailorProfile, User
from .views import should_cache_payload


class OrderFlowTests(APITestCase):
    @staticmethod
    def make_inline_image(size=(2400, 1800), color=(120, 80, 40)):
        output = io.BytesIO()
        Image.new('RGB', size, color).save(output, format='PNG')
        encoded = base64.b64encode(output.getvalue()).decode('ascii')
        return f'data:image/png;base64,{encoded}'

    def setUp(self):
        self.customer = User.objects.create_user(
            email='customer@example.com',
            password='password123',
            full_name='Customer User',
            role=User.Role.CUSTOMER,
            phone='03000000001',
            address='Customer Address',
        )
        self.tailor = User.objects.create_user(
            email='tailor@example.com',
            password='password123',
            full_name='Tailor User',
            role=User.Role.TAILOR,
            phone='03000000002',
            address='Tailor Address',
        )
        TailorProfile.objects.create(
            user=self.tailor,
            is_active=True,
            is_featured=True,
            service_price='25.00',
        )
        self.design = Design.objects.create(
            title='Classic Kandura',
            category='Formal',
            description='Clean cut',
            base_price='10.00',
            image='data:image/png;base64,AAAABBBB',
            images=[
                'data:image/png;base64,AAAABBBB',
                'https://cdn.example.com/designs/classic-kandura.png',
            ],
        )
        self.tailor_design = Design.objects.create(
            title='Tailor Portfolio Design',
            category='Formal',
            description='Tailor-owned sample',
            base_price='12.00',
            uploaded_by=self.tailor,
            image='data:image/png;base64,INLINEPORTFOLIO',
            images=[
                'data:image/png;base64,INLINEPORTFOLIO',
                'https://cdn.example.com/designs/tailor-portfolio.png',
            ],
        )
        self.fabric = Fabric.objects.create(
            material='Cotton',
            color='White',
            price='5.00',
            image='data:image/png;base64,CCCCDDDD',
            images=[
                'data:image/png;base64,CCCCDDDD',
                'https://cdn.example.com/fabrics/cotton-white.png',
            ],
        )
        self.measurement = MeasurementProfile.objects.create(
            customer=self.customer,
            name='Default Fit',
            chest='38.00',
            waist='34.00',
            shoulder='16.00',
            sleeve='22.00',
            height='70.00',
            length='45.00',
            is_default=True,
        )

    def test_customer_can_create_order_and_both_sides_can_see_it(self):
        self.client.force_authenticate(user=self.customer)
        response = self.client.post(
            '/api/orders/',
            {
                'tailor': self.tailor.id,
                'design': self.design.id,
                'fabric': self.fabric.id,
                'measurement_id': self.measurement.id,
                'payment_method': 'card',
                'delivery_address': 'Customer Address',
                'notes': 'Please keep the cuffs slim.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['tailor'], self.tailor.id)
        self.assertEqual(response.data['measurement']['id'], self.measurement.id)
        self.assertEqual(response.data['subtotal'], '50.00')
        self.assertEqual(response.data['total'], '50.00')

        customer_orders = self.client.get('/api/orders/')
        self.assertEqual(customer_orders.status_code, status.HTTP_200_OK)
        self.assertEqual(len(customer_orders.data), 1)
        self.assertEqual(customer_orders.data[0]['id'], response.data['id'])

        self.client.force_authenticate(user=self.tailor)
        tailor_orders = self.client.get('/api/tailor/orders/')
        self.assertEqual(tailor_orders.status_code, status.HTTP_200_OK)
        self.assertEqual(len(tailor_orders.data), 1)
        self.assertEqual(tailor_orders.data[0]['id'], response.data['id'])

    def test_customer_cannot_use_someone_elses_measurement(self):
        other_customer = User.objects.create_user(
            email='other@example.com',
            password='password123',
            full_name='Other Customer',
            role=User.Role.CUSTOMER,
        )
        foreign_measurement = MeasurementProfile.objects.create(
            customer=other_customer,
            name='Foreign Fit',
            chest='40.00',
            waist='35.00',
            shoulder='17.00',
            sleeve='23.00',
            height='71.00',
            length='46.00',
            is_default=True,
        )

        self.client.force_authenticate(user=self.customer)
        response = self.client.post(
            '/api/orders/',
            {
                'tailor': self.tailor.id,
                'measurement_id': foreign_measurement.id,
                'payment_method': 'card',
                'delivery_address': 'Customer Address',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('measurement_id', response.data)

    def test_tailor_endpoints_prefer_public_images_and_omit_inline_fallbacks_on_get(self):
        self.client.force_authenticate(user=self.customer)
        order_response = self.client.post(
            '/api/orders/',
            {
                'tailor': self.tailor.id,
                'design': self.design.id,
                'fabric': self.fabric.id,
                'measurement_id': self.measurement.id,
                'payment_method': 'card',
                'delivery_address': 'Customer Address',
            },
            format='json',
        )

        self.assertEqual(order_response.status_code, status.HTTP_201_CREATED)
        self.client.force_authenticate(user=self.tailor)

        designs_response = self.client.get('/api/tailor/designs/')
        self.assertEqual(designs_response.status_code, status.HTTP_200_OK)
        self.assertEqual(designs_response.data[0]['image'], 'https://cdn.example.com/designs/tailor-portfolio.png')
        self.assertEqual(designs_response.data[0]['images'], ['https://cdn.example.com/designs/tailor-portfolio.png'])

        orders_response = self.client.get('/api/tailor/orders/')
        self.assertEqual(orders_response.status_code, status.HTTP_200_OK)
        self.assertEqual(orders_response.data[0]['design_image'], 'https://cdn.example.com/designs/classic-kandura.png')
        self.assertEqual(orders_response.data[0]['fabric_image'], 'https://cdn.example.com/fabrics/cotton-white.png')
        self.assertEqual(orders_response.data[0]['design_images'], ['https://cdn.example.com/designs/classic-kandura.png'])
        self.assertEqual(orders_response.data[0]['fabric_images'], ['https://cdn.example.com/fabrics/cotton-white.png'])

        inline_design = Design.objects.create(
            title='Inline Only Design',
            category='Custom',
            description='Uploaded from app',
            base_price='15.00',
            uploaded_by=self.tailor,
            image='data:image/png;base64,INLINE_ONLY_DESIGN',
            images=['data:image/png;base64,INLINE_ONLY_DESIGN'],
        )
        inline_fabric = Fabric.objects.create(
            material='Inline Cotton',
            color='Cream',
            price='7.00',
            uploaded_by=self.tailor,
            image='data:image/png;base64,INLINE_ONLY_FABRIC',
            images=['data:image/png;base64,INLINE_ONLY_FABRIC'],
        )

        self.client.force_authenticate(user=self.customer)
        inline_order_response = self.client.post(
            '/api/orders/',
            {
                'tailor': self.tailor.id,
                'design': inline_design.id,
                'fabric': inline_fabric.id,
                'measurement_id': self.measurement.id,
                'payment_method': 'card',
                'delivery_address': 'Customer Address',
            },
            format='json',
        )
        self.assertEqual(inline_order_response.status_code, status.HTTP_201_CREATED)

        self.client.force_authenticate(user=self.tailor)
        updated_designs_response = self.client.get('/api/tailor/designs/')
        self.assertEqual(updated_designs_response.status_code, status.HTTP_200_OK)
        self.assertEqual(updated_designs_response.data[0]['image'], '')
        self.assertEqual(updated_designs_response.data[0]['images'], [])

        updated_orders_response = self.client.get('/api/tailor/orders/')
        self.assertEqual(updated_orders_response.status_code, status.HTTP_200_OK)
        self.assertEqual(updated_orders_response.data[0]['design_image'], '')
        self.assertEqual(updated_orders_response.data[0]['fabric_image'], '')
        self.assertEqual(updated_orders_response.data[0]['design_images'], [])
        self.assertEqual(updated_orders_response.data[0]['fabric_images'], [])

    def test_tailor_can_create_design_without_category_field(self):
        self.client.force_authenticate(user=self.tailor)
        response = self.client.post(
            '/api/tailor/designs/',
            {
                'title': 'No Category Design',
                'description': 'Uploaded without category',
                'base_price': '200.00',
                'image': 'data:image/jpeg;base64,AAAABBBB',
                'images': ['data:image/jpeg;base64,AAAABBBB'],
                'compatible_fabrics': [],
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['category'], 'Custom')

    def test_admin_order_list_includes_design_and_fabric_images(self):
        admin = User.objects.create_superuser(
            email='admin@example.com',
            password='password123',
            full_name='Admin User',
        )
        order = Order.objects.create(
            customer=self.customer,
            tailor=self.tailor,
            design=self.design,
            fabric=self.fabric,
            measurement=self.measurement,
            customer_phone=self.customer.phone,
            delivery_address=self.customer.address,
            subtotal='40.00',
            total='40.00',
        )

        self.client.force_authenticate(user=admin)
        response = self.client.get('/api/admin/orders/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['id'], order.id)
        self.assertEqual(response.data[0]['design_image'], 'https://cdn.example.com/designs/classic-kandura.png')
        self.assertEqual(response.data[0]['fabric_image'], 'https://cdn.example.com/fabrics/cotton-white.png')
        self.assertEqual(response.data[0]['design_images'], ['https://cdn.example.com/designs/classic-kandura.png'])
        self.assertEqual(response.data[0]['fabric_images'], ['https://cdn.example.com/fabrics/cotton-white.png'])

    def test_admin_driver_summary_returns_compact_assignment_payload(self):
        admin = User.objects.create_superuser(
            email='admin-summary@example.com',
            password='password123',
            full_name='Admin Summary',
        )
        driver = User.objects.create_user(
            email='driver@example.com',
            password='password123',
            full_name='Driver User',
            role=User.Role.DRIVER,
            phone='03000000009',
            address='Driver Address',
        )
        from .models import DriverProfile

        DriverProfile.objects.create(
            user=driver,
            vehicle_type='Bike',
            vehicle_number='ABC-123',
            license_number='LIC-1',
            is_available=True,
        )

        self.client.force_authenticate(user=admin)
        response = self.client.get('/api/admin/drivers/?summary=1')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['id'], driver.id)
        self.assertEqual(response.data[0]['name'], 'Driver User')
        self.assertEqual(response.data[0]['phone'], '03000000009')
        self.assertEqual(response.data[0]['vehicle_type'], 'Bike')
        self.assertTrue(response.data[0]['is_available'])
        self.assertNotIn('recent_deliveries', response.data[0])
        self.assertNotIn('email', response.data[0])

    def test_tailor_order_detail_includes_design_and_fabric_images(self):
        order = Order.objects.create(
            customer=self.customer,
            tailor=self.tailor,
            design=self.design,
            fabric=self.fabric,
            measurement=self.measurement,
            customer_phone=self.customer.phone,
            delivery_address=self.customer.address,
            subtotal='40.00',
            total='40.00',
        )

        self.client.force_authenticate(user=self.tailor)
        response = self.client.get(f'/api/tailor/orders/{order.id}/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['design_image'], 'https://cdn.example.com/designs/classic-kandura.png')
        self.assertEqual(response.data['fabric_image'], 'https://cdn.example.com/fabrics/cotton-white.png')
        self.assertEqual(response.data['design_images'], ['https://cdn.example.com/designs/classic-kandura.png'])
        self.assertEqual(response.data['fabric_images'], ['https://cdn.example.com/fabrics/cotton-white.png'])

    def test_tailor_order_list_stays_summary_only_for_dashboard_and_lists(self):
        order = Order.objects.create(
            customer=self.customer,
            tailor=self.tailor,
            design=self.design,
            fabric=self.fabric,
            measurement=self.measurement,
            customer_phone=self.customer.phone,
            delivery_address=self.customer.address,
            subtotal='40.00',
            total='40.00',
            notes='Keep the cuffs slim.',
        )

        self.client.force_authenticate(user=self.tailor)
        response = self.client.get('/api/tailor/orders/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['id'], order.id)
        self.assertEqual(response.data[0]['design_images'], ['https://cdn.example.com/designs/classic-kandura.png'])
        self.assertEqual(response.data[0]['fabric_images'], ['https://cdn.example.com/fabrics/cotton-white.png'])
        self.assertNotIn('measurement', response.data[0])
        self.assertNotIn('delivery', response.data[0])

    def test_tailor_order_list_can_bypass_cached_payloads_for_live_refresh(self):
        Order.objects.create(
            customer=self.customer,
            tailor=self.tailor,
            design=self.design,
            fabric=self.fabric,
            measurement=self.measurement,
            customer_phone=self.customer.phone,
            delivery_address=self.customer.address,
            subtotal='40.00',
            total='40.00',
        )

        self.client.force_authenticate(user=self.tailor)
        initial_response = self.client.get('/api/tailor/orders/')

        self.assertEqual(initial_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(initial_response.data), 1)

        Order.objects.create(
            customer=self.customer,
            tailor=self.tailor,
            design=self.design,
            fabric=self.fabric,
            measurement=self.measurement,
            customer_phone=self.customer.phone,
            delivery_address=self.customer.address,
            subtotal='40.00',
            total='40.00',
        )

        cached_response = self.client.get('/api/tailor/orders/')
        fresh_response = self.client.get('/api/tailor/orders/', HTTP_X_BYPASS_CACHE='1')

        self.assertEqual(cached_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(cached_response.data), 1)
        self.assertEqual(fresh_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(fresh_response.data), 2)

    def test_customer_dashboard_tailor_list_omits_inline_tailor_logo(self):
        tailor_with_inline_logo = User.objects.create_user(
            email='inline-tailor@example.com',
            password='password123',
            full_name='Inline Tailor',
            role=User.Role.TAILOR,
            phone='03000000003',
            address='Inline Address',
        )
        TailorProfile.objects.create(
            user=tailor_with_inline_logo,
            shop_name='Inline Logo Shop',
            image='data:image/png;base64,INLINE_TAILOR_LOGO',
            specialty='kandura',
            location='Sharjah',
            is_featured=True,
            is_active=True,
        )

        self.client.force_authenticate(user=self.customer)
        response = self.client.get('/api/dashboard/customer/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        inline_tailor = next(item for item in response.data['top_tailors'] if item['id'] == tailor_with_inline_logo.id)
        self.assertEqual(inline_tailor['image'], '')

    def test_public_tailor_list_excludes_private_profile_fields(self):
        TailorProfile.objects.filter(user=self.tailor).update(
            bank_name='Secret Bank',
            account_number='123456789',
            iban='PK00TEST000000000000',
            national_id='35202-0000000-0',
        )

        response = self.client.get('/api/tailors/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]['id'], self.tailor.id)
        self.assertNotIn('bank_name', response.data[0])
        self.assertNotIn('account_number', response.data[0])
        self.assertNotIn('iban', response.data[0])
        self.assertNotIn('national_id', response.data[0])

    def test_public_fabric_detail_returns_public_image_list(self):
        response = self.client.get(f'/api/fabrics/{self.fabric.id}/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['image'], 'https://cdn.example.com/fabrics/cotton-white.png')
        self.assertEqual(response.data['images'], ['https://cdn.example.com/fabrics/cotton-white.png'])

    def test_customer_dashboard_omits_inline_catalog_images_and_keeps_order_summary_light(self):
        inline_design = Design.objects.create(
            title='Inline Dashboard Design',
            category='Custom',
            description='Inline image should stay visible in the dashboard',
            base_price='15.00',
            image='data:image/png;base64,INLINE_ONLY_DESIGN',
            images=['data:image/png;base64,INLINE_ONLY_DESIGN'],
            is_active=True,
        )
        inline_fabric = Fabric.objects.create(
            material='Inline Dashboard Fabric',
            color='Cream',
            price='7.00',
            image='data:image/png;base64,INLINE_ONLY_FABRIC',
            images=['data:image/png;base64,INLINE_ONLY_FABRIC'],
            is_active=True,
        )
        order = Order.objects.create(
            customer=self.customer,
            tailor=self.tailor,
            design=inline_design,
            fabric=inline_fabric,
            measurement=self.measurement,
            customer_phone=self.customer.phone,
            delivery_address=self.customer.address,
            subtotal='47.00',
            total='47.00',
        )

        self.client.force_authenticate(user=self.customer)
        response = self.client.get('/api/dashboard/customer/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        dashboard_design = next(item for item in response.data['designs'] if item['id'] == inline_design.id)
        dashboard_fabric = next(item for item in response.data['fabrics'] if item['id'] == inline_fabric.id)
        dashboard_order = next(item for item in response.data['recent_orders'] if item['id'] == order.id)

        self.assertEqual(dashboard_design['image'], '')
        self.assertEqual(dashboard_design['images'], [])
        self.assertEqual(dashboard_fabric['image'], '')
        self.assertEqual(dashboard_fabric['images'], [])
        self.assertNotIn('measurement', dashboard_order)
        self.assertNotIn('delivery', dashboard_order)
        self.assertEqual(
            set(dashboard_order.keys()),
            {'id', 'tailor_name', 'design_name', 'status', 'total', 'created_at'},
        )

    def test_cache_skips_inline_or_oversized_payloads(self):
        self.assertFalse(should_cache_payload({'image': 'data:image/png;base64,INLINE_BIG_IMAGE'}))
        self.assertFalse(should_cache_payload({'blob': 'x' * 300000}))
        self.assertTrue(should_cache_payload({'detail': 'ok', 'count': 2}))

    def test_backend_silently_optimizes_uploaded_inline_images(self):
        large_inline_image = self.make_inline_image()

        self.client.force_authenticate(user=self.tailor)
        design_response = self.client.post(
            '/api/tailor/designs/',
            {
                'title': 'Compressed Design',
                'description': 'Should be optimized on save',
                'base_price': '150.00',
                'image': large_inline_image,
                'images': [large_inline_image],
                'compatible_fabrics': [],
            },
            format='json',
        )
        self.assertEqual(design_response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(str(design_response.data['image']).startswith('data:image/jpeg;base64,'))
        self.assertLess(len(design_response.data['image']), len(large_inline_image))

        setup_response = self.client.patch(
            '/api/tailor/me/',
            {
                'shop_name': 'Compressed Logo Shop',
                'image': large_inline_image,
            },
            format='json',
        )
        self.assertEqual(setup_response.status_code, status.HTTP_200_OK)
        self.assertTrue(str(setup_response.data['image']).startswith('data:image/jpeg;base64,'))
        self.assertLess(len(setup_response.data['image']), len(large_inline_image))

    @override_settings(MAX_API_REQUEST_BODY_SIZE=1024)
    def test_backend_rejects_oversized_request_bodies_before_processing(self):
        self.client.force_authenticate(user=self.tailor)
        response = self.client.post(
            '/api/tailor/designs/',
            {
                'title': 'Too Large',
                'description': 'Should fail fast',
                'base_price': '150.00',
                'image': 'x' * 5000,
                'compatible_fabrics': [],
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        self.assertIn('too large', str(response.json()['detail']).lower())

    def test_backend_rejects_too_many_inline_images(self):
        large_inline_image = self.make_inline_image(size=(100, 100))

        self.client.force_authenticate(user=self.tailor)
        response = self.client.post(
            '/api/tailor/designs/',
            {
                'title': 'Too Many Images',
                'description': 'Should not accept more than the image limit',
                'base_price': '150.00',
                'images': [large_inline_image] * 7,
                'compatible_fabrics': [],
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('images', response.data)

    @override_settings(MAX_API_FORM_FIELDS=3, DATA_UPLOAD_MAX_NUMBER_FIELDS=3)
    def test_backend_rejects_requests_with_too_many_fields(self):
        self.client.force_authenticate(user=self.tailor)
        response = self.client.patch(
            '/api/tailor/me/',
            {
                'shop_name': 'Field Flood Shop',
                'full_name': 'Tailor User',
                'phone': '03000000002',
                'address': 'Tailor Address',
            },
            format='multipart',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('too many fields', str(response.json()['detail']).lower())

    def test_backend_normalizes_duplicate_and_blank_image_references(self):
        self.client.force_authenticate(user=self.tailor)
        inline_image = self.make_inline_image(size=(100, 100))

        response = self.client.post(
            '/api/tailor/designs/',
            {
                'title': 'Normalized Images',
                'description': 'Should return clean image arrays',
                'base_price': '150.00',
                'image': f'  {inline_image}  ',
                'images': ['   ', inline_image, inline_image, f'  {inline_image}  '],
                'compatible_fabrics': [],
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['images'], [response.data['image']])

    def test_backend_uses_first_valid_image_when_primary_image_missing(self):
        design = Design.objects.create(
            title='Image Fallback Design',
            category='Custom',
            description='Uses image list fallback',
            base_price='15.00',
            uploaded_by=self.tailor,
            image='   ',
            images=['   ', 'https://cdn.example.com/designs/fallback.png', 'https://cdn.example.com/designs/fallback.png'],
        )

        self.client.force_authenticate(user=self.tailor)
        response = self.client.get('/api/tailor/designs/')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payload = next(item for item in response.data if item['id'] == design.id)
        self.assertEqual(payload['image'], 'https://cdn.example.com/designs/fallback.png')
        self.assertEqual(payload['images'], ['https://cdn.example.com/designs/fallback.png'])
