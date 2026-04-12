from rest_framework import status
from rest_framework.test import APITestCase

from .models import Design, Fabric, MeasurementProfile, TailorProfile, User


class OrderFlowTests(APITestCase):
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

    def test_tailor_endpoints_strip_inline_base64_images(self):
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
