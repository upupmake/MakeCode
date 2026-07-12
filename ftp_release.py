"""
FTP 上传脚本 - 将构建产物上传到更新服务器。
用法: python ftp_release.py

需要在同目录下创建 .ftp_config 文件，格式为 JSON：
{
    "host": "xxx.xxx.xxx.xxx",
    "port": 21,
    "user": "username",
    "pass": "password"
}
"""
import json
import sys
from ftplib import FTP
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / ".ftp_config"

FILES = [
    "dist/MakeCode-Windows-X64.zip",
    "dist/version.json",
]


class NatFTP(FTP):
    """修复 NAT 环境下 PASV 返回内网 IP 的问题，强制使用公网 IP。"""

    def makepasv(self):
        _, port = super().makepasv()
        return self.host, port


def load_config() -> dict:
    """加载 FTP 配置。"""
    if not CONFIG_FILE.exists():
        print(f"❌ 未找到配置文件 {CONFIG_FILE}")
        print('   请创建 .ftp_config 文件，内容示例：')
        print('   {"host": "xxx.xxx.xxx.xxx", "port": 21, "user": "username", "pass": "password"}')
        sys.exit(1)

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    required = ["host", "port", "user", "pass"]
    missing = [k for k in required if k not in config]
    if missing:
        print(f"❌ 配置文件缺少字段: {', '.join(missing)}")
        sys.exit(1)

    return config


def upload_file(ftp: FTP, local_path: Path):
    """上传单个文件到 FTP 当前目录。"""
    size_mb = local_path.stat().st_size / 1024 / 1024
    print(f"  上传 {local_path.name} ({size_mb:.1f} MB) ...", end=" ", flush=True)
    with open(local_path, "rb") as f:
        ftp.storbinary(f"STOR {local_path.name}", f)
    print("OK")


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    config = load_config()

    # 检查文件是否存在
    missing = [f for f in FILES if not Path(f).exists()]
    if missing:
        print(f"❌ 以下文件不存在: {', '.join(missing)}")
        print("请先运行 pyinstaller 和 release.py")
        sys.exit(1)

    print(f"连接 FTP {config['host']}:{config['port']} ...")
    ftp = NatFTP()
    ftp.connect(config["host"], config["port"], timeout=30)
    ftp.login(config["user"], config["pass"])
    ftp.set_pasv(True)
    print("登录成功")

    for f in FILES:
        upload_file(ftp, Path(f))

    ftp.quit()
    print()
    print("[OK] 上传完成！")

    # 清理发布日志文件
    log_file = Path("RELEASE_LOG.md")
    if log_file.exists():
        log_file.unlink()


if __name__ == "__main__":
    main()
