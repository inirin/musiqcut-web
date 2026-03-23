"""
python run.py 한 번으로 서버 실행
"""
import subprocess
import sys
import os
import webbrowser
import time
from pathlib import Path

VENV_DIR = Path(__file__).parent / "venv"


def get_python():
    """venv 또는 시스템 Python 반환"""
    venv_py = VENV_DIR / "Scripts" / "python.exe"
    if venv_py.exists():
        return str(venv_py)
    return sys.executable


def get_uvicorn():
    venv_uv = VENV_DIR / "Scripts" / "uvicorn.exe"
    if venv_uv.exists():
        return str(venv_uv)
    return "uvicorn"


def check_env():
    env_file = Path(".env")
    if not env_file.exists():
        print("=" * 60)
        print("[!] .env 파일이 없습니다!")
        print("=" * 60)
        print()
        print(".env.example 을 복사해서 .env 를 만들고 API 키를 입력하세요.")
        print()
        create = input("지금 .env 파일을 생성할까요? (y/n): ").strip().lower()
        if create == 'y':
            import shutil
            shutil.copy(".env.example", ".env")
            print("[OK] .env 파일이 생성되었습니다. 파일을 열어 API 키를 입력하세요.")
            if sys.platform == "win32":
                os.startfile(".env")
        print()


def setup_venv():
    if not VENV_DIR.exists():
        print("[...] 가상환경 생성 중...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)
        print("[OK] 가상환경 생성 완료")

    print("[...] 의존성 설치 중...")
    pip = str(VENV_DIR / "Scripts" / "pip.exe")
    subprocess.run(
        [pip, "install", "-r", "requirements.txt", "-q"],
        check=True
    )
    print("[OK] 의존성 준비 완료")


def ensure_dirs():
    for folder in ["storage/projects", "storage/temp"]:
        Path(folder).mkdir(parents=True, exist_ok=True)


def main():
    script_dir = Path(__file__).parent
    os.chdir(script_dir)

    print()
    print("MusiqCut - AI Music Video Pipeline")
    print("=" * 40)

    check_env()
    setup_venv()
    ensure_dirs()

    port = int(os.environ.get("APP_PORT", 8000))
    url = f"http://localhost:{port}"

    print(f"\n[>>] 서버 시작: {url}")
    print("     Ctrl+C 로 종료\n")

    import threading
    def open_browser():
        time.sleep(2)
        webbrowser.open(url)
    threading.Thread(target=open_browser, daemon=True).start()

    subprocess.run([
        get_uvicorn(),
        "backend.main:app",
        "--host", "0.0.0.0",
        "--port", str(port),
        "--reload"
    ])


if __name__ == "__main__":
    main()
