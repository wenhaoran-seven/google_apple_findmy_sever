"""Concurrent readers for queries; exclusive account/device mutations."""
import asyncio
from contextlib import asynccontextmanager


class QueryGate:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.readers = 0
        self.writer = False
        self.waiting_writers = 0

    async def __aenter__(self):
        async with self.condition:
            self.waiting_writers += 1
            try:
                await self.condition.wait_for(lambda: not self.writer and not self.readers)
                self.writer = True
            finally:
                self.waiting_writers -= 1
                self.condition.notify_all()
        return self

    async def __aexit__(self, *exc):
        async with self.condition:
            self.writer = False
            self.condition.notify_all()

    @asynccontextmanager
    async def shared(self):
        async with self.condition:
            await self.condition.wait_for(lambda: not self.writer and not self.waiting_writers)
            self.readers += 1
        try:
            yield
        finally:
            async with self.condition:
                self.readers -= 1
                self.condition.notify_all()
