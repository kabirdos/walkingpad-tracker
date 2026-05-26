class DaemonState:
    """Shared state between the capture loop and the HTTP API.

    Holds the store (history), the latest live PadStatus, the connection flag,
    and delegates control commands to the pad client (sole BLE owner).
    """

    def __init__(self, store, pad_client=None):
        self.store = store
        self.pad_client = pad_client
        self.latest_status = None
        self.connected = False

    def record_status(self, status):
        self.latest_status = status

    async def wake(self):
        if self.pad_client is not None:
            await self.pad_client.wake()

    async def start_belt(self):
        if self.pad_client is not None:
            await self.pad_client.start_belt()

    async def stop_belt(self):
        if self.pad_client is not None:
            await self.pad_client.stop_belt()

    async def set_speed(self, kmh):
        if self.pad_client is not None:
            await self.pad_client.set_speed(kmh)
