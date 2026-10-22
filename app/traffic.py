"""Générateur de trafic : simule des utilisateurs qui appellent orders-api en continu."""
import os
import random
import time

import requests

TARGET = os.environ.get("TARGET", "http://app:8000")

while True:
    try:
        if random.random() < 0.7:
            requests.get(f"{TARGET}/api/orders", timeout=5)
        else:
            requests.post(f"{TARGET}/api/orders", json={"amount": random.randint(50, 900)}, timeout=5)
    except requests.RequestException as exc:
        print(f"[traffic] erreur: {exc}", flush=True)
    time.sleep(random.uniform(0.1, 0.5))
