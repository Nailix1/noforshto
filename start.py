"""Единый запуск: Mini App + Admin bot + Client bot."""
import os, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def spawn(script):
    return subprocess.Popen([sys.executable, '-u', str(ROOT / script)],
                            cwd=str(ROOT), env=os.environ.copy())


def main():
    scripts = ['server.py', 'admin_bot.py', 'client_bot.py']
    procs = [spawn(s) for s in scripts]
    try:
        while True:
            for i, p in enumerate(procs):
                if p.poll() is not None:
                    print(f'Процесс {scripts[i]} упал, перезапускаю…', flush=True)
                    procs[i] = spawn(scripts[i])
            time.sleep(5)
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()


if __name__ == '__main__':
    main()