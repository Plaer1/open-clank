"""Bounded process-local observations; never starts or probes a server."""
from collections import OrderedDict
import threading
import time

class McpObservations:
    def __init__(self, limit=128):
        self._limit = limit
        self._serial = 0
        self._items = OrderedDict()
        self._lock = threading.Lock()

    def begin(self, server_id):
        with self._lock:
            self._serial += 1
            generation = self._serial
            previous = self._items.pop(server_id, {})
            self._items[server_id] = {'generation': generation, 'status': 'connecting', 'observed_at': time.time(), 'discover_ms': None,
                                      'last_call_ms': previous.get('last_call_ms'), 'last_call_at': previous.get('last_call_at'),
                                      'last_call_generation': previous.get('last_call_generation'), 'last_error_category': previous.get('last_error_category')}
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
            return generation, time.monotonic()

    def update(self, server_id, generation, *, status=None, discovery_ms=None, call_started=None, error=None):
        with self._lock:
            item = self._items.get(server_id)
            if not item or item['generation'] != generation:
                return
            if status is not None: item['status'] = status
            if isinstance(discovery_ms, int) and discovery_ms >= 0: item['discover_ms'] = discovery_ms
            if call_started is not None:
                item['last_call_ms'] = max(0, round((time.monotonic()-call_started)*1000))
                item['last_call_at'] = time.time()
                item['last_call_generation'] = generation
                item['last_error_category'] = error
            item['observed_at'] = time.time()

    def get(self, server_id):
        with self._lock:
            item = self._items.get(server_id)
            return dict(item) if item else None
