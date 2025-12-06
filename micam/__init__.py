import os
import asyncio
import aiohttp
import argparse
import logging
import subprocess
import threading
import queue
import time
import sys
import signal
from typing import Optional

# 配置日志
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("Bridge")

class PipeWriter(threading.Thread):
    """独立线程负责写入命名管道，防止阻塞"""
    def __init__(self, pipe_path, name):
        super().__init__(daemon=True)
        self.pipe_path = pipe_path
        self.name = name
        self.queue = queue.Queue(maxsize=1000)
        self.fd = None
        self.running = True
        self._ensure_pipe()

    def _ensure_pipe(self):
        try:
            if os.path.exists(self.pipe_path):
                os.remove(self.pipe_path)
            os.mkfifo(self.pipe_path)
            # O_RDWR 防止 Linux/macOS 上 open 阻塞
            self.fd = os.open(self.pipe_path, os.O_RDWR)
            logger.info(f"[{self.name}] Pipe opened: {self.pipe_path}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to create/open pipe: {e}")
            self.running = False

    def write(self, data):
        if not self.running: return
        try:
            self.queue.put(data, timeout=0.01)
        except queue.Full:
            pass 

    def run(self):
        while self.running:
            try:
                data = self.queue.get(timeout=1.0)
                if self.fd:
                    os.write(self.fd, data)
            except queue.Empty:
                continue
            except OSError as e:
                logger.error(f"[{self.name}] Write error: {e}")
                break
            except Exception as e:
                logger.error(f"[{self.name}] Error: {e}")
                break
        self.close()

    def close(self):
        self.running = False
        if self.fd:
            try: os.close(self.fd)
            except: pass
            self.fd = None
        if os.path.exists(self.pipe_path):
            try: os.remove(self.pipe_path)
            except: pass

class RTSPBridge:
    def __init__(self, base_url, username, password, camera_id, rtsp_url, video_codec, channel, video_quality):
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password
        self.camera_id = camera_id
        self.channel = str(channel)
        self.video_quality = str(video_quality)
        self.video_codec = video_codec
        self.rtsp_url = rtsp_url
        self.process: Optional[subprocess.Popen] = None
        
        self.pipe_video = f"/tmp/miot_video_{camera_id}.pipe"
        self.pipe_audio = f"/tmp/miot_audio_{camera_id}.pipe"
        
        self.video_writer = None
        self.audio_writer = None

    async def _login(self, session) -> bool:
        try:
            await session.post(f"{self.base_url}/api/auth/login", 
                             json={"username": self.username, "password": self.password}, ssl=False)
            async with session.get(f"{self.base_url}/api/miot/login_status", ssl=False) as r:
                return r.status == 200
        except Exception as e:
            logger.error(f"Login error: {e}")
            return False

    def _start_ffmpeg(self):
        self.video_writer = PipeWriter(self.pipe_video, "Video")
        self.audio_writer = PipeWriter(self.pipe_audio, "Audio")
        self.video_writer.start()
        self.audio_writer.start()

        ffmpeg_cmd = [
            'ffmpeg',
            '-y',
            '-v', 'error', # 生产模式只看错误
            '-hide_banner',
            
            # [全局时间戳控制]
            '-use_wallclock_as_timestamps', '1',
            '-fflags', '+genpts+nobuffer', 
            '-flags', 'low_delay',
            '-analyzeduration', '1000000', 
            '-probesize', '1000000',       

            # --- 输入 1: 视频 (裸流) ---
            '-f', self.video_codec, 
            '-use_wallclock_as_timestamps', '1', # 视频依赖 Wallclock
            '-i', self.pipe_video,

            # --- 输入 2: 音频 (G.711A) ---
            '-f', 'alaw', 
            '-ar', '16000', # 锁定 16k (小米高清常见配置)
            '-ac', '1',
            # [关键] 音频不使用 Wallclock，依赖下方滤镜重构
            '-i', self.pipe_audio,

            # --- 映射 ---
            '-map', '0:v',
            '-map', '1:a',

            # --- 编码与处理 ---
            
            # 视频: 透传 (Copy) - 不消耗 CPU
            '-c:v', 'copy', 
            '-bsf:v', 'hevc_mp4toannexb', 

            # 音频: PCM (s16le) - 极低 CPU
            # [关键] 使用 asetpts 根据样本计数重写时间戳，消除网络抖动带来的延迟
            '-af', 'aresample=16000,asetpts=N/SR/TB',
            
            '-c:a', 'pcm_s16le', 
            '-ar', '16000',     # 输出 16k 给 Go2RTC
            '-ac', '1',

            # --- 输出 RTSP ---
            '-f', 'rtsp',
            '-rtsp_transport', 'tcp',
            self.rtsp_url,
        ]

        logger.info("Starting FFmpeg (PCM Output, Low CPU)...")
        self.process = subprocess.Popen(
            ffmpeg_cmd, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.PIPE
        )
        
        threading.Thread(target=self._monitor_ffmpeg, daemon=True).start()

    def _monitor_ffmpeg(self):
        if not self.process: return
        for line in self.process.stderr:
            l = line.decode(errors='ignore').strip()
            if "Error" in l:
                logger.error(f"[FFmpeg] {l}")

    def _stop_ffmpeg(self):
        if self.video_writer: self.video_writer.close()
        if self.audio_writer: self.audio_writer.close()

        if self.process:
            self.process.terminate()
            try: self.process.wait(timeout=2)
            except: self.process.kill()
            self.process = None

    async def run_forever(self):
        while True:
            try:
                await self.run_session()
            except Exception as e:
                logger.error(f"Session error: {e}")
            logger.info("Restarting bridge in 3s...")
            self._stop_ffmpeg()
            await asyncio.sleep(3)

    async def run_session(self):
        self._start_ffmpeg()
        jar = aiohttp.CookieJar(unsafe=True)
        timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=20)

        async with aiohttp.ClientSession(cookie_jar=jar, timeout=timeout) as session:
            if not await self._login(session): return

            protocol = "wss" if self.base_url.startswith("https") else "ws"
            host = self.base_url.split("://")[1]
            ws_url = f"{protocol}://{host}/api/miot/ws/video_stream?camera_id={self.camera_id}&channel={self.channel}&video_quality={self.video_quality}"
            
            logger.info(f"Connecting to WS: {ws_url}")
            async with session.ws_connect(ws_url, ssl=False, heartbeat=15.0) as ws:
                logger.info("WebSocket Connected! Streaming...")
                
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        data = msg.data
                        if len(data) > 1:
                            p_type = data[0]
                            payload = data[1:]
                            if p_type == 1: self.video_writer.write(payload)
                            elif p_type == 2: self.audio_writer.write(payload)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        logger.info("WS Closed")
                        break

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("MILOCO_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default=os.getenv("MILOCO_PASSWORD", ""))
    parser.add_argument("--camera-id", default=os.getenv("CAMERA_ID", ""))
    # 确保这里的 IP 是你 HAOS 的 IP
    parser.add_argument("--rtsp-url", default=os.getenv("RTSP_URL", "rtsp://127.0.0.1:8554/stream1"))
    parser.add_argument("--video-quality", default="2")
    
    args = parser.parse_args()
    if not args.password: return

    bridge = RTSPBridge(
        base_url=args.base_url,
        username=args.username,
        password=args.password,
        camera_id=args.camera_id,
        rtsp_url=args.rtsp_url,
        video_codec="hevc",
        channel=0,
        video_quality=args.video_quality
    )

    try:
        asyncio.run(bridge.run_forever())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
