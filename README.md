# 🎦 RTSP bridge for Xiaomi Camera


## Install

### Docker compose
```shell
mkdir /opt/micam
cd /opt/micam
wget https://raw.githubusercontent.com/miiot/micam/refs/heads/main/docker-compose.yml
docker compose up -d
```

> 此命令会通过docker部署Miloco、Go2rtc及RTSP转发服务。如果需要添加多个摄像头，需要编辑`docker-compose.yml`运行多个micam服务。
>
> 部署的Miloco为基础版，不带AI引擎，无GPU算力要求，大部分机器都能运行，但目前不支持arm架构。


## Usage

### [Miloco](https://github.com/XiaoMi/xiaomi-miloco)

1. Open Miloco WebUI: `https://192.168.1.xx:8000`
2. Set miloco password
3. Bind your Xiaomi account
4. Camera offline ? [[Xiaomi Miloco Q&A]](https://github.com/XiaoMi/xiaomi-miloco/issues/56)


### [Go2rtc](https://github.com/AlexxIT/go2rtc)

1. Open Go2rtc WebUI: `http://192.168.1.xx:1984`
2. Config empty streams:
   ```yaml
   streams:
      your_stream1:
      your_stream2:
   ```
3. Save & Restart


### Micam

1. Set environment variables:
   ```shell
   cat << EOF > .env
   MILOCO_PASSWORD=your_miloco_password_md5
   CAMERA_ID=1234567890 # your camera did
   RTSP_URL=rtsp://go2rtc:8554/your_stream1
   EOF
   ```
2. Restart micam: `docker compose restart micam1`
