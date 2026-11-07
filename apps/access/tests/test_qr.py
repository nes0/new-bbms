from django.test import TestCase, override_settings

from apps.access import qr
from apps.access.models import Device
from apps.members.models import Member


def decode(raw: bytes):
    """Read the raw format the way a microcontroller would: N, then N packed rows."""
    size = raw[0]
    row_bytes = (size + 7) // 8
    rows = []
    for y in range(size):
        row = raw[1 + y * row_bytes : 1 + (y + 1) * row_bytes]
        rows.append([bool(row[x // 8] & (0x80 >> (x % 8))) for x in range(size)])
    return size, rows


@override_settings(SITE_URL="https://bbms.buildbrighton.com")
class QrCodeApiTests(TestCase):
    def setUp(self):
        self.laser = Device(name="Red laser", device_id="laser", role=Device.Role.EQUIPMENT)
        self.key = self.laser.generate_api_key()
        self.laser.save()
        self.url = "https://bbms.buildbrighton.com/d/laser"

    def get(self, **kwargs):
        headers = {"Authorization": f"Bearer {self.key}", **kwargs.pop("headers", {})}
        return self.client.get("/api/devices/v2/qr", headers=headers, **kwargs)

    def test_raw_format_decodes_to_the_same_code(self):
        response = self.get()
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertEqual(response["X-QR-URL"], self.url)
        size, rows = decode(response.content)
        expected = [[bool(m) for m in row] for row in qr.make(self.url).matrix_iter(border=0)]
        self.assertEqual(size, len(expected))
        self.assertEqual(rows, expected)
        self.assertEqual(len(response.content), 1 + size * ((size + 7) // 8))

    def test_typical_code_is_small(self):
        size = self.get().content[0]
        self.assertEqual(size, 29)  # version 3: fits a small OLED at 2 pixels per module

    def test_json_format(self):
        data = self.get(data={"format": "json"}).json()
        self.assertEqual(data["url"], self.url)
        self.assertEqual(len(data["rows"]), data["size"])
        self.assertEqual(bytes.fromhex(data["rows"][0]), qr.packed_rows(self.url)[1][0])

    def test_etag_gives_304_when_unchanged(self):
        etag = self.get()["ETag"]
        self.assertEqual(self.get(headers={"If-None-Match": etag}).status_code, 304)

    def test_v1_api_key_header_also_works(self):
        response = self.client.get("/api/devices/v2/qr", headers={"ApiKey": self.key})
        self.assertEqual(response.status_code, 200)

    def test_unknown_device_key_is_401(self):
        response = self.client.get("/api/devices/v2/qr", headers={"Authorization": "Bearer nope"})
        self.assertEqual(response.status_code, 401)


class DevicePageTests(TestCase):
    def setUp(self):
        self.laser = Device(name="Red laser", device_id="laser", role=Device.Role.EQUIPMENT)
        self.laser.generate_api_key()
        self.laser.save()

    def test_short_link_redirects_to_the_device_page(self):
        response = self.client.get("/d/laser")
        self.assertRedirects(response, "/devices/laser/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/d/nothing").status_code, 404)

    def test_device_page_needs_login(self):
        self.assertEqual(self.client.get("/devices/laser/").status_code, 302)
        member = Member.objects.create_user("kim@example.com", "a-good-password", given_name="Kim", family_name="K")
        self.client.force_login(member)
        self.assertContains(self.client.get("/devices/laser/"), "Red laser")
