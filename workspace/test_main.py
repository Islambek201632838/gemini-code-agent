import unittest
from fastapi.testclient import TestClient

from main import app


class TestMainApp(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_read_root(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"message": "Добро пожаловать в Stats API!"})

    def test_mean(self):
        response = self.client.post("/mean", json={"numbers": [1, 2, 3]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": 2.0})

    def test_mean_empty(self):
        response = self.client.post("/mean", json={"numbers": []})
        self.assertEqual(response.status_code, 400)

    def test_median(self):
        response = self.client.post("/median", json={"numbers": [1, 3, 2]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": 2.0})

    def test_median_empty(self):
        response = self.client.post("/median", json={"numbers": []})
        self.assertEqual(response.status_code, 400)

    def test_clamp(self):
        response = self.client.post("/clamp", json={"value": 15, "low": 0, "high": 10})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": 10.0})

    def test_clamp_below(self):
        response = self.client.post("/clamp", json={"value": -5, "low": 0, "high": 10})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": 0.0})


if __name__ == "__main__":
    unittest.main()
