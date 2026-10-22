"""API와 웹 프록시가 응답 대기 중 연결 종료를 같은 방식으로 처리한다."""
import asyncio
from starlette.responses import StreamingResponse
from starlette.requests import ClientDisconnect


class DisconnectAwareStreamingResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        # ASGI 2.4의 send 오류만 기다리면 토큰 대기 중 끊긴 연결을 감지하지 못한다.
        async def send_or_disconnect(message):
            try:
                await send(message)
            except OSError as exc:
                # 소켓 전송 실패만 종료로 분류하고 생성기·상류 I/O 오류는 전파한다.
                raise ClientDisconnect() from exc

        # 전송과 disconnect 수신을 동시에 기다린다. 한쪽이 끝나면 다른 쪽을 취소·회수해야 작업이 남지 않는다.
        tasks = [asyncio.create_task(self.stream_response(send_or_disconnect)),
                 asyncio.create_task(self.listen_for_disconnect(receive))]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                try:
                    task.result()
                except ClientDisconnect:
                    pass
        finally:
            # 취소를 한 번 전달하고 정리를 기다려 LangGraph의 하위 작업까지 종료한다.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # send에서 멈춘 생성기는 yield 직후 정지해 있을 수 있어 task 취소 외에 aclose도 필요하다.
            close = getattr(self.body_iterator, 'aclose', None)
            if close is not None:
                await close()
        if self.background is not None:
            await self.background()
