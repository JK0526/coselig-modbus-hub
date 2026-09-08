import asyncio
import unittest
from sunwave.protocol import crc16, frame, read, write, validate, ProtocolError, DeviceException
from sunwave.transport import Transport
from sunwave.devices import Channel, validate_channels


class ProtocolTests(unittest.TestCase):
    def test_known_crc(self):
        self.assertEqual(crc16(bytes.fromhex('01030000000a')), 0xCDC5)

    def test_response_validation(self):
        request = write(51,2090,50)
        self.assertEqual(validate(request,request),request)
        for invalid in (write(52,2090,50),write(51,2090,51),request[:-1]+bytes([request[-1]^1])):
            with self.assertRaises(ProtocolError): validate(request,invalid)
        with self.assertRaises(DeviceException): validate(request,frame(bytes([51,0x86,2])))
        with self.assertRaises(ProtocolError): validate(read(51,4),frame(bytes([51,3,4,0,1,0,2])))

    def test_inventory(self):
        channels = [
            Channel('p404', 1, 'a', 'dual', 'test dual'),
            Channel('p404', 1, '3', 'single', 'test single'),
            Channel('p210', 2, '1', 'single', 'test p210'),
            Channel('U4', 3, '1', 'single', 'test U4'),
        ]
        validate_channels(channels)
        self.assertEqual(len(channels), 4)
        self.assertEqual(len({c.slave for c in channels}), 3)
        with self.assertRaises(ValueError): validate_channels(channels+[channels[0]])

    def test_state_never_becomes_command(self):
        c=Channel('p404',54,'a','dual','test')
        for raw in (0,25,50,75,100):
            states=c.states([50,raw,0,0])
            self.assertFalse(any('/set' in t for t in states))
            command=c.temperature_command(int(states[c.topic+'/colortemp']))
            self.assertAlmostEqual(command[5],raw,delta=1)
        self.assertEqual(c.brightness_command(1)[5],2)
        self.assertEqual(c.brightness_command(0)[5],0)
        with self.assertRaises(ValueError): c.brightness_command(101)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connections=0
        self.requests=[]
        self.handlers=set()
        self.mode='normal'
        self.release=asyncio.Event()
        self.arrived=asyncio.Event()
        async def handle(reader,writer):
            task=asyncio.current_task(); self.handlers.add(task)
            self.connections+=1
            try:
                while True:
                    request=await reader.readexactly(8)
                    self.requests.append(request)
                    self.arrived.set()
                    if self.mode=='block': await self.release.wait()
                    response=request if request[1]==6 else frame(bytes([request[0],3,8,0,50,0,25,0,0,0,0]))
                    # Explicitly fragment the header and body across TCP writes.
                    writer.write(response[:1]); await writer.drain()
                    await asyncio.sleep(0.001)
                    writer.write(response[1:]); await writer.drain()
            except (asyncio.IncompleteReadError,ConnectionError): pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionError:
                    pass
                self.handlers.discard(task)
        self.server=await asyncio.start_server(handle,'127.0.0.1',0)
        self.transport=Transport('127.0.0.1',self.server.sockets[0].getsockname()[1],timeout=.2,capacity=2)

    async def asyncTearDown(self):
        await self.transport.close()
        self.server.close()
        for task in list(self.handlers): task.cancel()
        await asyncio.gather(*list(self.handlers),return_exceptions=True)
        await self.server.wait_closed()

    async def test_fragmented_persistent_connection(self):
        for request in (read(51,4),write(51,2090,20)):
            validate(request,await self.transport.submit(request))
        self.assertEqual(self.connections,1)

    async def test_priority_and_poll_deduplication(self):
        self.mode='block'
        first=asyncio.create_task(self.transport.submit(read(51,4),poll=True))
        await self.arrived.wait()
        self.assertIsNone(await self.transport.submit(read(51,4),poll=True))
        poll=asyncio.create_task(self.transport.submit(read(52,4),poll=True))
        await asyncio.sleep(0)
        control=asyncio.create_task(self.transport.submit(write(51,2090,70)))
        await asyncio.sleep(0)
        with self.assertRaises(asyncio.QueueFull): await self.transport.submit(write(51,2090,71))
        self.release.set()
        await asyncio.gather(first,poll,control)
        self.assertEqual([r[1] for r in self.requests],[3,6,3])

    async def test_timeout_reconnects(self):
        self.mode='block'
        with self.assertRaises(TimeoutError): await self.transport.submit(read(51,4))
        self.mode='normal'; self.release.set()
        request=write(51,2090,30)
        self.assertEqual(await self.transport.submit(request),request)
        self.assertEqual(self.connections,2)

    async def test_close_cancels_pending(self):
        self.mode='block'
        active=asyncio.create_task(self.transport.submit(read(51,4)))
        await self.arrived.wait()
        pending=asyncio.create_task(self.transport.submit(read(52,4)))
        await asyncio.sleep(0)
        await self.transport.close()
        results=await asyncio.gather(active,pending,return_exceptions=True)
        self.assertTrue(all(isinstance(r,asyncio.CancelledError) for r in results))

    async def test_coalesced_frames(self):
        from sunwave.protocol import receive
        reader=asyncio.StreamReader()
        a=write(51,2090,20); b=write(52,2090,30)
        reader.feed_data(a+b)
        self.assertEqual(await receive(reader),a)
        self.assertEqual(await receive(reader),b)
