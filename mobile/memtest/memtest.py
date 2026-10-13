"""Peak-memory breakdown of llama-server on the phone, one stage at a time.

Starts the app's own llama-server binary (run-as com.idox.mobile) with the app's
options, forwards its port to this laptop, sends a text request and two scan-page
requests, and prints VmHWM / VmRSS / RssAnon / RssFile after each stage.

  python3 memtest.py <model.gguf> <mmproj.gguf> <img1.png> <img2.png> [extra server args...]
"""
import base64
import json
import subprocess
import sys
import time
import urllib.request

PKG = "com.idox.mobile"
PORT = 39999
MODELS = "/data/user/0/com.idox.mobile/files/models/"


def sh(cmd):
    return subprocess.run(["adb", "shell", cmd], capture_output=True, text=True).stdout


def libdir():
    path = sh(f"pm path {PKG}").strip().split(":", 1)[1]
    return path.rsplit("/", 1)[0] + "/lib/arm64"


def mem(pid):
    out = sh(f"run-as {PKG} cat /proc/{pid}/status")
    vals = {}
    for line in out.splitlines():
        k = line.split(":")[0]
        if k in ("VmHWM", "VmRSS", "RssAnon", "RssFile"):
            vals[k] = int(line.split()[1]) // 1024
    return vals


def post(path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read())


def main():
    model, mmproj, img1, img2, extra = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:]
    lib = libdir()
    args = [f"{lib}/libllama-server.so", "-m", MODELS + model, "--mmproj", MODELS + mmproj,
            "--host", "127.0.0.1", "--port", str(PORT), "-t", "4", "-tb", "4",
            "-Cr", "4-7", "-Crb", "4-7", "--cpu-strict", "1", "--cpu-strict-batch", "1",
            "-fa", "on", "-np", "1", "-c", "8192", "-ctk", "q8_0", "-ctv", "q8_0",
            "-ngl", "0", "--no-mmproj-offload", "--no-op-offload"] + extra
    sh(f"am force-stop {PKG}")
    sh(f"run-as {PKG} pkill -f libllama-server.so")
    time.sleep(2)
    cmd = (f"cd /data/user/0/{PKG}/files && LD_LIBRARY_PATH={lib} "
           f"LLAMA_ARG_IMAGE_MIN_TOKENS=512 LLAMA_ARG_IMAGE_MAX_TOKENS=512 "
           + " ".join(args) + " > memtest_server.log 2>&1 < /dev/null &")
    subprocess.Popen(["adb", "shell", f"run-as {PKG} sh -c '{cmd}'"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["adb", "forward", f"tcp:{PORT}", f"tcp:{PORT}"])
    for _ in range(120):
        time.sleep(1)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2)
            break
        except Exception:
            pass
    pid = sh(f"run-as {PKG} pidof libllama-server.so").split()[0]
    print("args:", " ".join(extra) or "(app defaults)")
    print("loaded       ", mem(pid))

    marker = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/props").read())["media_marker"]
    tmpl = "<|im_start|>user\n{}<|im_end|>\n<|im_start|>assistant\n"
    post("/completion", {"prompt": tmpl.format("Say hello."), "n_predict": 20, "temperature": 0})
    print("text request ", mem(pid))

    for n, img in enumerate([img1, img2], 1):
        b64 = base64.b64encode(open(img, "rb").read()).decode()
        t = time.time()
        r = post("/completion", {"prompt": {"prompt_string": tmpl.format(marker + "Write all the text on this page."),
                                            "multimodal_data": [b64]},
                                 "n_predict": 200, "temperature": 0})
        print(f"scan page {n}  ", mem(pid), f"{time.time() - t:.0f}s, prompt tokens {r['timings']['prompt_n']}")

    sh(f"run-as {PKG} kill {pid}")


if __name__ == "__main__":
    main()
