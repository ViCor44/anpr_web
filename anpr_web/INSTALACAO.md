# ANPR VIGI — Guia de instalação num Raspberry Pi novo

Sistema de reconhecimento automático de matrículas com:
- Câmara IP (RTSP)
- Deteção de veículos (YOLOv8)
- Deteção e OCR de matrículas
- Controlo de portão via relé (GPIO)
- Interface web (Flask) com login
- Gestão de matrículas autorizadas
- Histórico de eventos com screenshots
- Botão manual de abertura com registo de IP

---

## 1. Hardware necessário

- Raspberry Pi 4 ou 5 (mínimo 4 GB RAM recomendado)
- Cartão microSD ≥ 32 GB (Classe 10/A2)
- Fonte de alimentação oficial
- Módulo de relé 5V (1 canal, ativo a LOW)
- Câmara IP com stream RTSP (testado com Dahua/Tapo)
- Cabos jumper fêmea-fêmea (para o relé)
- Rede com IP fixo ou DHCP estável

### Ligação do relé ao Pi

| Relé | Pi (BCM) | Pino físico |
|------|----------|-------------|
| VCC  | 5V       | pino 2      |
| GND  | GND      | pino 6      |
| IN   | GPIO 17  | pino 11     |

---

## 2. Instalação do sistema operativo

1. Grava **Raspberry Pi OS (64-bit, Bookworm)** com o Raspberry Pi Imager
2. No Imager, configura logo:
   - Hostname: `anpr-vigi`
   - Utilizador: `desenvolvimento` / password à escolha
   - Wi-Fi ou prepara Ethernet
   - SSH ativado
3. Boota o Pi e atualiza:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

---

## 3. Dependências de sistema

```bash
sudo apt install -y \
    python3-pip python3-venv python3-dev \
    libatlas-base-dev libopenblas-dev \
    libjpeg-dev libtiff-dev libpng-dev \
    libavcodec-dev libavformat-dev libswscale-dev \
    libv4l-dev libxvidcore-dev libx264-dev \
    libgtk-3-dev libgstreamer1.0-dev \
    ffmpeg sqlite3 git nano
```

---

## 4. Ambiente Python

```bash
cd ~
python3 -m venv anpr_env
source ~/anpr_env/bin/activate
pip install --upgrade pip setuptools wheel
```

A partir daqui, **todos os comandos `pip` e `python3` devem ter o ambiente ativo**. Verifica que o prompt mostra `(anpr_env)`.

---

## 5. Estrutura de pastas

```bash
mkdir -p ~/anpr_web/templates ~/anpr_web/static ~/anpr_web/data/snapshots
cd ~/anpr_web
```

Estrutura final:

```text
~/anpr_web/
├── app.py
├── requirements.txt
├── templates/
│   ├── index.html
│   ├── history.html
│   └── plates.html
├── static/
│   ├── style.css
│   └── app.js
└── data/
    ├── events.db          (criado automaticamente)
    └── snapshots/
```

---

## 6. Ficheiro `requirements.txt`

```bash
nano ~/anpr_web/requirements.txt
```

Conteúdo:

```text
flask
opencv-python
ultralytics
fast-plate-ocr
open-image-models
onnxruntime
numpy
pillow
rpi-lgpio
```

Instalar:

```bash
cd ~/anpr_web
pip install -r requirements.txt
```

> ⚠️ A primeira instalação pode demorar **15–30 minutos** (compilação de OpenCV/numpy).
> Se aparecerem erros de memória, aumenta o swap:
> ```bash
> sudo dphys-swapfile swapoff
> sudo nano /etc/dphys-swapfile     # CONF_SWAPSIZE=2048
> sudo dphys-swapfile setup
> sudo dphys-swapfile swapon
> ```

---

## 7. Aplicação principal — `app.py`

```bash
nano ~/anpr_web/app.py
```

Cola todo o conteúdo abaixo. **Altera as 6 constantes marcadas com `⚠️ EDITAR`**.

```python
import os
import re
import cv2
import time
import sqlite3
import threading
from pathlib import Path
from datetime import datetime
from functools import wraps

from flask import (
    Flask, Response, render_template, request, jsonify,
    redirect, url_for, session, send_from_directory
)

# ===================== CONFIG =====================
APP_HOST = "0.0.0.0"
APP_PORT = 8080

SECRET_KEY   = "TROCA_ESTA_CHAVE_LONGA_E_ALEATORIA"   # ⚠️ EDITAR
WEB_PASSWORD = "1234"                                 # ⚠️ EDITAR

CAM_IP   = "192.168.1.100"      # ⚠️ EDITAR IP da câmara
CAM_USER = "admin"              # ⚠️ EDITAR
CAM_PASS = "password_da_camara" # ⚠️ EDITAR
CAM_PORT = 554

# Ajusta os paths do RTSP conforme o modelo da câmara
RTSP_MAIN = f"rtsp://{CAM_USER}:{CAM_PASS}@{CAM_IP}:{CAM_PORT}/stream1"
RTSP_SUB  = f"rtsp://{CAM_USER}:{CAM_PASS}@{CAM_IP}:{CAM_PORT}/stream2"
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"

RELE_PIN         = 17
RELE_ATIVO_BAIXO = True
TEMPO_RELE_SEG   = 2.0

BASE_DIR = Path.home() / "anpr_web"
DATA_DIR = BASE_DIR / "data"
SNAP_DIR = DATA_DIR / "snapshots"
DB_PATH  = DATA_DIR / "events.db"
VALIDAS_PATH = Path.home() / "matriculas_validas.txt"

for p in [DATA_DIR, SNAP_DIR]:
    p.mkdir(parents=True, exist_ok=True)

# ===================== GPIO =====================
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    GPIO_AVAILABLE = False

# ===================== ANPR =====================
ANPR_ENABLED = True
try:
    import numpy as np
    from ultralytics import YOLO
    from fast_plate_ocr import LicensePlateRecognizer
    from open_image_models import LicensePlateDetector
except Exception as e:
    print(f"[WARN] ANPR libs não disponíveis: {e}")
    ANPR_ENABLED = False

MODEL_VEHICLE       = "yolov8n.pt"
VEHICLE_CLASSES     = {2, 3, 5, 7}
CONF_VEHICLE        = 0.45
CONF_PLATE          = 0.25
OCR_MODEL           = "global-plates-mobile-vit-v2-model"
YOLO_IMGSZ          = 320
DETECTAR_CADA_N     = 5
ANPR_COOLDOWN_S     = 10
ANPR_MIN_CONF_SAVE  = 0.95   # ⚠️ só guarda/abre acima desta confiança

REGEX_MATRICULA = re.compile(
    r"^("
    r"[A-Z]{2}\d{2}\d{2}|"
    r"\d{2}\d{2}[A-Z]{2}|"
    r"\d{2}[A-Z]{2}\d{2}|"
    r"[A-Z]{2}\d{2}[A-Z]{2}|"
    r"\d{4}[A-Z]{3}"
    r")$"
)

# ===================== APP =====================
app = Flask(__name__)
app.secret_key = SECRET_KEY


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("auth"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            event_type TEXT NOT NULL,
            plate TEXT,
            confidence REAL,
            authorized INTEGER,
            client_ip TEXT,
            user_agent TEXT,
            snapshot_path TEXT,
            note TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate TEXT NOT NULL UNIQUE,
            label TEXT,
            added_at TEXT NOT NULL,
            added_by_ip TEXT,
            added_by_ua TEXT,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()


def add_event(event_type, plate=None, confidence=None, authorized=None,
              client_ip_value=None, user_agent=None, snapshot_path=None, note=None):
    conn = db()
    conn.execute("""
        INSERT INTO events
        (ts, event_type, plate, confidence, authorized, client_ip, user_agent, snapshot_path, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        now_iso(), event_type, plate, confidence,
        None if authorized is None else (1 if authorized else 0),
        client_ip_value, user_agent, snapshot_path, note
    ))
    conn.commit()
    conn.close()


def list_events(limit=20):
    conn = db()
    rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def load_valid_plates():
    plates = set()
    if VALIDAS_PATH.exists():
        for line in VALIDAS_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                plates.add(line.strip().upper().replace("-", "").replace(" ", ""))
    else:
        VALIDAS_PATH.write_text("# uma matricula por linha\n", encoding="utf-8")
    try:
        conn = db()
        rows = conn.execute("SELECT plate FROM plates WHERE active=1").fetchall()
        conn.close()
        for r in rows:
            plates.add(r["plate"].upper().replace("-", "").replace(" ", ""))
    except Exception as e:
        print(f"[plates] erro: {e}")
    return plates


class GPIOController:
    def __init__(self, pin, ativo_baixo=True):
        self.pin = pin
        self.ativo_baixo = ativo_baixo
        if GPIO_AVAILABLE:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(self.pin, GPIO.OUT,
                       initial=GPIO.HIGH if ativo_baixo else GPIO.LOW)

    def acionar(self, segundos):
        if GPIO_AVAILABLE:
            GPIO.output(self.pin, GPIO.LOW if self.ativo_baixo else GPIO.HIGH)
            time.sleep(segundos)
            GPIO.output(self.pin, GPIO.HIGH if self.ativo_baixo else GPIO.LOW)
        else:
            time.sleep(segundos)


class RTSPStream:
    def __init__(self, url, nome=""):
        self.url = url
        self.nome = nome
        self.frame = None
        self.lock = threading.Lock()
        self.running = False
        self.cap = None
        self._abrir()

    def _abrir(self):
        print(f"[RTSP {self.nome}] abrir")
        self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        try:
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

    def start(self):
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def _loop(self):
        falhas = 0
        while self.running:
            if not self.cap or not self.cap.isOpened():
                time.sleep(2)
                self._abrir()
                continue
            ret, frame = self.cap.read()
            if not ret:
                falhas += 1
                if falhas > 30:
                    try: self.cap.release()
                    except Exception: pass
                    self.cap = None
                    falhas = 0
                continue
            falhas = 0
            with self.lock:
                self.frame = frame

    def read(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()


stream_sub  = RTSPStream(RTSP_SUB,  "sub").start()
stream_main = RTSPStream(RTSP_MAIN, "main").start()
gpio        = GPIOController(RELE_PIN, RELE_ATIVO_BAIXO)

anpr_latest = {"plate": None, "confidence": None, "authorized": None, "ts": None}
valid_plates = load_valid_plates()


def save_snapshot(frame, prefix="event"):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{ts}.jpg"
    path = SNAP_DIR / filename
    cv2.imwrite(str(path), frame)
    return f"data/snapshots/{filename}"


def open_gate(seconds=TEMPO_RELE_SEG):
    threading.Thread(target=gpio.acionar, args=(seconds,), daemon=True).start()


def mjpeg_generator():
    while True:
        frame = stream_sub.read()
        if frame is None:
            time.sleep(0.1)
            continue
        ok, jpg = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            continue
        yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg.tobytes() + b"\r\n")
        time.sleep(0.05)


# ===================== ANPR =====================
class ANPREngine:
    def __init__(self):
        self.enabled = ANPR_ENABLED
        self.running = False
        self.processando = False
        self.frame_count = 0
        self.ultimo_tempo = 0
        self.ultima_matricula = None
        if not self.enabled:
            return
        print("[ANPR] a carregar modelos...")
        self.model_vehicle  = YOLO(MODEL_VEHICLE)
        self.plate_detector = LicensePlateDetector(
            detection_model="yolo-v9-t-384-license-plate-end2end"
        )
        self.ocr = LicensePlateRecognizer(OCR_MODEL)
        print("[ANPR] pronto")

    def _tight_crop(self, img, thresh=30):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        mask = gray > thresh
        if not mask.any():
            return img
        rows = np.any(mask, axis=1)
        cols = np.any(mask, axis=0)
        r0, r1 = np.where(rows)[0][[0, -1]]
        c0, c1 = np.where(cols)[0][[0, -1]]
        r0 = max(0, r0 - 2); c0 = max(0, c0 - 2)
        r1 = min(img.shape[0], r1 + 3); c1 = min(img.shape[1], c1 + 3)
        return img[r0:r1, c0:c1]

    def _ocr_candidates(self, plate_img):
        plate_img = self._tight_crop(plate_img)
        ph, pw = plate_img.shape[:2]
        target_w = 256
        if pw > 0 and pw != target_w:
            s = target_w / pw
            plate_img = cv2.resize(plate_img, (target_w, max(32, int(ph * s))),
                                   interpolation=cv2.INTER_CUBIC)
        plate_gray = cv2.cvtColor(plate_img, cv2.COLOR_BGR2GRAY)
        resultado = self.ocr.run(plate_gray, return_confidence=True)
        preds = resultado if isinstance(resultado, list) else [resultado]
        candidatos = []
        for pred in preds:
            if hasattr(pred, "plate"):
                texto = pred.plate
                try: conf = float(np.mean(pred.char_probs))
                except Exception: conf = 0.5
            elif isinstance(pred, tuple) and len(pred) == 2:
                texto, confs = pred
                try: conf = float(np.mean(confs))
                except Exception: conf = 0.5
            else:
                texto = str(pred); conf = 0.5
            limpo = re.sub(r"[^A-Z0-9]", "", str(texto).upper())
            if 4 <= len(limpo) <= 9:
                candidatos.append((limpo, conf, plate_img))
        return candidatos

    def _recognize(self, frame):
        rv = self.model_vehicle(frame, imgsz=640, conf=CONF_VEHICLE, verbose=False)[0]
        rois = []
        h, w = frame.shape[:2]
        for box in rv.boxes:
            if int(box.cls[0]) in VEHICLE_CLASSES:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                x1 = max(0, x1 - 10); y1 = max(0, y1 - 10)
                x2 = min(w, x2 + 10); y2 = min(h, y2 + 10)
                rois.append(frame[y1:y2, x1:x2])
        if not rois:
            rois = [frame]
        candidatos = []
        best_crop = None
        for roi in rois:
            for det in self.plate_detector.predict(roi):
                if float(det.confidence) < CONF_PLATE:
                    continue
                bb = det.bounding_box
                px1, py1, px2, py2 = int(bb.x1), int(bb.y1), int(bb.x2), int(bb.y2)
                px1 = max(0, px1 - 6); py1 = max(0, py1 - 6)
                px2 = min(roi.shape[1], px2 + 6); py2 = min(roi.shape[0], py2 + 6)
                plate_img = roi[py1:py2, px1:px2]
                if plate_img.size == 0:
                    continue
                for texto, conf, processed in self._ocr_candidates(plate_img):
                    candidatos.append((texto, conf))
                    if best_crop is None:
                        best_crop = processed
        candidatos = [(t, c) for (t, c) in candidatos if c >= 0.70]
        candidatos.sort(key=lambda x: x[1], reverse=True)
        for txt, c in candidatos:
            if REGEX_MATRICULA.match(txt):
                return txt, c, best_crop
        if candidatos:
            return candidatos[0][0], candidatos[0][1], best_crop
        return None, 0.0, best_crop

    def _processar(self):
        try:
            frame = stream_main.read()
            if frame is None:
                frame = stream_sub.read()
            if frame is None:
                return
            plate, conf, plate_crop = self._recognize(frame)
            if not plate:
                return
            if conf < ANPR_MIN_CONF_SAVE:
                print(f"[ANPR] ignorado {plate} (conf {conf:.2f} < {ANPR_MIN_CONF_SAVE})")
                return
            agora = time.time()
            if self.ultima_matricula == plate and (agora - self.ultimo_tempo) < ANPR_COOLDOWN_S:
                return
            self.ultima_matricula = plate
            self.ultimo_tempo = agora
            authorized = plate in valid_plates
            snap = save_snapshot(frame, "anpr")
            if authorized:
                open_gate()
            add_event(
                event_type="anpr_authorized" if authorized else "anpr_denied",
                plate=plate, confidence=conf, authorized=authorized,
                snapshot_path=snap, note="Reconhecimento ANPR"
            )
            anpr_latest.update({
                "plate": plate, "confidence": conf,
                "authorized": authorized, "ts": now_iso()
            })
            if plate_crop is not None:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                cv2.imwrite(str(SNAP_DIR / f"plate_{ts}.jpg"), plate_crop)
        except Exception as e:
            print(f"[ANPR] erro: {e}")
        finally:
            self.processando = False

    def loop(self):
        if not self.enabled:
            return
        self.running = True
        while self.running:
            try:
                frame = stream_sub.read()
                if frame is None:
                    time.sleep(0.1); continue
                self.frame_count += 1
                if self.frame_count % DETECTAR_CADA_N != 0:
                    time.sleep(0.01); continue
                res = self.model_vehicle(frame, imgsz=YOLO_IMGSZ, conf=CONF_VEHICLE, verbose=False)[0]
                veiculo = any(int(box.cls[0]) in VEHICLE_CLASSES for box in res.boxes)
                if veiculo and not self.processando:
                    self.processando = True
                    threading.Thread(target=self._processar, daemon=True).start()
                time.sleep(0.02)
            except Exception as e:
                print(f"[ANPR loop] erro: {e}")
                time.sleep(1)

    def start(self):
        if self.enabled:
            threading.Thread(target=self.loop, daemon=True).start()


anpr_engine = ANPREngine()


# ===================== ROUTES =====================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == WEB_PASSWORD:
            session["auth"] = True
            return redirect(url_for("index"))
        return render_template("index.html", login_mode=True, error="Password errada")
    return render_template("index.html", login_mode=True, error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html", login_mode=False)


@app.route("/history")
@login_required
def history():
    return render_template("history.html")


@app.route("/plates")
@login_required
def plates_page():
    return render_template("plates.html")


@app.route("/video_feed")
@login_required
def video_feed():
    return Response(mjpeg_generator(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/status")
@login_required
def api_status():
    return jsonify({
        "camera_ip": CAM_IP,
        "anpr_enabled": ANPR_ENABLED,
        "latest": anpr_latest
    })


@app.route("/api/events")
@login_required
def api_events():
    limit = int(request.args.get("limit", 20))
    return jsonify(list_events(limit))


@app.route("/api/events/all")
@login_required
def api_events_all():
    limit = int(request.args.get("limit", 500))
    return jsonify(list_events(limit))


@app.route("/api/open_gate", methods=["POST"])
@login_required
def api_open_gate():
    frame = stream_main.read()
    if frame is None:
        frame = stream_sub.read()
    snap = save_snapshot(frame, "manual") if frame is not None else None
    ip = client_ip()
    ua = request.headers.get("User-Agent", "")
    open_gate()
    add_event(
        event_type="manual_open", authorized=True,
        client_ip_value=ip, user_agent=ua,
        snapshot_path=snap, note="Abertura manual via UI web"
    )
    return jsonify({"ok": True, "client_ip": ip, "snapshot": snap})


@app.route("/api/plates", methods=["GET"])
@login_required
def api_plates_list():
    conn = db()
    rows = conn.execute("""
        SELECT id, plate, label, added_at, added_by_ip, active
        FROM plates ORDER BY id DESC
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/plates", methods=["POST"])
@login_required
def api_plates_add():
    global valid_plates
    data = request.get_json(silent=True) or request.form
    raw = (data.get("plate") or "").upper()
    plate = re.sub(r"[^A-Z0-9]", "", raw)
    label = (data.get("label") or "").strip()
    if len(plate) < 4 or len(plate) > 9:
        return jsonify({"ok": False, "error": "Matrícula inválida"}), 400
    ip = client_ip()
    ua = request.headers.get("User-Agent", "")
    conn = db()
    try:
        conn.execute("""
            INSERT INTO plates (plate, label, added_at, added_by_ip, added_by_ua, active)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (plate, label or None, now_iso(), ip, ua))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"ok": False, "error": "Matrícula já existe"}), 409
    conn.close()
    valid_plates = load_valid_plates()
    add_event(
        event_type="plate_added", plate=plate, authorized=True,
        client_ip_value=ip, user_agent=ua,
        note=f"Matrícula adicionada{(' — ' + label) if label else ''}"
    )
    return jsonify({"ok": True, "plate": plate})


@app.route("/api/plates/<int:pid>", methods=["DELETE"])
@login_required
def api_plates_delete(pid):
    global valid_plates
    ip = client_ip()
    ua = request.headers.get("User-Agent", "")
    conn = db()
    row = conn.execute("SELECT plate FROM plates WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "Não encontrada"}), 404
    plate = row["plate"]
    conn.execute("DELETE FROM plates WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    valid_plates = load_valid_plates()
    add_event(
        event_type="plate_removed", plate=plate, authorized=False,
        client_ip_value=ip, user_agent=ua, note="Matrícula removida"
    )
    return jsonify({"ok": True})


@app.route("/api/reload_plates", methods=["POST"])
@login_required
def api_reload_plates():
    global valid_plates
    valid_plates = load_valid_plates()
    return jsonify({"ok": True, "count": len(valid_plates)})


@app.route("/data/snapshots/<path:filename>")
@login_required
def serve_snapshot(filename):
    return send_from_directory(SNAP_DIR, filename)


@app.route("/health")
def health():
    return "ok", 200


if __name__ == "__main__":
    init_db()
    anpr_engine.start()
    app.run(host=APP_HOST, port=APP_PORT, debug=False, threaded=True)
```

---

## 8. Templates HTML

### `templates/index.html`

```bash
nano ~/anpr_web/templates/index.html
```

```html
<!doctype html>
<html lang="pt">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ANPR VIGI</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  {% if login_mode %}
  <div class="login-wrap">
    <form class="card login-card" method="post" action="/login">
      <h1>ANPR VIGI</h1>
      <p>Entrar na interface web</p>
      {% if error %}<div class="error">{{ error }}</div>{% endif %}
      <input type="password" name="password" placeholder="Password">
      <button type="submit">Entrar</button>
    </form>
  </div>
  {% else %}
  <header class="topbar">
    <div>
      <h1>ANPR VIGI</h1>
      <div class="muted" id="camera-ip">Câmara: --</div>
    </div>
    <div class="top-actions">
      <a class="linkbtn secondary" href="/plates">Gerir matrículas</a>
      <a class="linkbtn secondary" href="/logout">Sair</a>
    </div>
  </header>

  <main class="layout">
    <section class="left">
      <div class="card">
        <div class="card-header">
          <h2>Live</h2>
          <span class="badge" id="live-badge">online</span>
        </div>
        <img id="live-feed" src="/video_feed" alt="Live camera feed">
      </div>
      <div class="grid">
        <div class="card stat">
          <div class="muted">Última matrícula</div>
          <div class="big" id="latest-plate">--</div>
        </div>
        <div class="card stat">
          <div class="muted">Confiança</div>
          <div class="big" id="latest-confidence">--</div>
        </div>
        <div class="card stat">
          <div class="muted">Estado</div>
          <div class="big" id="latest-state">--</div>
        </div>
      </div>
    </section>

    <section class="right">
      <div class="card">
        <div class="card-header"><h2>Controlo</h2></div>
        <button id="open-gate-btn" class="danger">Abrir portão</button>
        <div class="muted top-space" id="manual-result">Nenhuma ação manual ainda.</div>
      </div>
      <div class="card">
        <div class="card-header">
          <h2>Últimos eventos</h2>
          <a class="linkbtn secondary" href="/history">Ver todos</a>
        </div>
        <div id="events"></div>
      </div>
    </section>
  </main>
  <script src="/static/app.js"></script>
  {% endif %}
</body>
</html>
```

### `templates/history.html`

```bash
nano ~/anpr_web/templates/history.html
```

```html
<!doctype html>
<html lang="pt">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Histórico — ANPR VIGI</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header class="topbar">
    <div>
      <h1>Histórico de eventos</h1>
      <div class="muted">Todos os snapshots e leituras</div>
    </div>
    <div class="top-actions">
      <a class="linkbtn secondary" href="/">← Voltar</a>
      <a class="linkbtn secondary" href="/logout">Sair</a>
    </div>
  </header>

  <main style="padding:20px">
    <div class="card">
      <div class="card-header">
        <h2>Eventos</h2>
        <div class="muted" id="counter">A carregar…</div>
      </div>
      <div id="events"></div>
    </div>
  </main>

  <div id="lightbox" onclick="this.style.display='none'"
       style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);
              z-index:9999;align-items:center;justify-content:center;cursor:zoom-out">
    <img id="lightbox-img" style="max-width:95vw;max-height:95vh;border-radius:8px">
  </div>

  <script>
    function fmtConfidence(v){ return v==null ? "--" : `${Math.round(v*100)}%`; }
    function eventTitle(ev){
      if(ev.event_type==="manual_open") return "Abertura manual";
      if(ev.event_type==="anpr_authorized") return "ANPR autorizado";
      if(ev.event_type==="anpr_denied") return "ANPR negado";
      if(ev.event_type==="plate_added") return "Matrícula adicionada";
      if(ev.event_type==="plate_removed") return "Matrícula removida";
      return ev.event_type;
    }
    function eventClass(ev){
      if(ev.event_type==="manual_open") return "warn";
      if(ev.event_type==="plate_added") return "ok";
      if(ev.event_type==="plate_removed") return "warn";
      if(ev.authorized===1) return "ok";
      if(ev.authorized===0) return "bad";
      return "";
    }
    function openLightbox(src){
      document.getElementById("lightbox-img").src = src;
      document.getElementById("lightbox").style.display = "flex";
    }
    async function load(){
      const r = await fetch("/api/events/all?limit=500");
      const events = await r.json();
      document.getElementById("counter").textContent = `${events.length} eventos`;
      const el = document.getElementById("events");
      el.innerHTML = "";
      for(const ev of events){
        const row = document.createElement("div");
        row.className = "event";
        const img = ev.snapshot_path
          ? `<img src="/${ev.snapshot_path}" alt="snapshot" style="cursor:zoom-in" onclick="openLightbox('/${ev.snapshot_path}')">`
          : `<div style="width:96px;height:72px;background:#000;border-radius:8px"></div>`;
        row.innerHTML = `
          ${img}
          <div>
            <div class="event-title ${eventClass(ev)}">${eventTitle(ev)}</div>
            <div class="muted">${ev.ts}</div>
            ${ev.plate ? `<div>Matrícula: <strong>${ev.plate}</strong></div>` : ""}
            ${ev.confidence!=null ? `<div>Confiança: ${fmtConfidence(ev.confidence)}</div>` : ""}
            ${ev.client_ip ? `<div>IP origem: ${ev.client_ip}</div>` : ""}
            ${ev.note ? `<div class="muted">${ev.note}</div>` : ""}
          </div>
        `;
        el.appendChild(row);
      }
    }
    load();
  </script>
</body>
</html>
```

### `templates/plates.html`

```bash
nano ~/anpr_web/templates/plates.html
```

```html
<!doctype html>
<html lang="pt">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Matrículas — ANPR VIGI</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <header class="topbar">
    <div>
      <h1>Matrículas autorizadas</h1>
      <div class="muted" id="plates-count">--</div>
    </div>
    <div class="top-actions">
      <a class="linkbtn secondary" href="/">← Voltar</a>
      <a class="linkbtn secondary" href="/logout">Sair</a>
    </div>
  </header>

  <main style="padding:20px;max-width:900px;margin:0 auto">
    <div class="card">
      <div class="card-header"><h2>Adicionar matrícula</h2></div>
      <form id="plate-form" class="plate-form">
        <input type="text" id="plate-input" placeholder="Ex: 6659NHG" maxlength="12" required>
        <input type="text" id="label-input" placeholder="Dono / nota (opcional)">
        <button type="submit">Adicionar</button>
      </form>
      <div class="muted" id="plate-msg" style="margin-top:8px"></div>
    </div>

    <div class="card" style="margin-top:20px">
      <div class="card-header"><h2>Lista</h2></div>
      <div id="plates-list"></div>
    </div>
  </main>

  <script>
    async function getJSON(url, opts={}){
      const r = await fetch(url, opts);
      if(!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    }
    async function loadPlates(){
      const plates = await getJSON("/api/plates");
      document.getElementById("plates-count").textContent = `${plates.length} ativas`;
      const el = document.getElementById("plates-list");
      el.innerHTML = "";
      if(plates.length === 0){
        el.innerHTML = `<div class="muted">Sem matrículas autorizadas.</div>`;
        return;
      }
      for(const p of plates){
        const row = document.createElement("div");
        row.className = "plate-row";
        row.innerHTML = `
          <div class="info">
            <div class="plate">${p.plate}</div>
            <div class="muted">
              ${p.label ? p.label + " · " : ""}
              adicionada ${p.added_at}${p.added_by_ip ? " · IP " + p.added_by_ip : ""}
            </div>
          </div>
          <button data-id="${p.id}" data-plate="${p.plate}">Remover</button>
        `;
        row.querySelector("button").addEventListener("click", async (ev)=>{
          if(!confirm(`Remover ${ev.target.dataset.plate}?`)) return;
          try{
            await getJSON(`/api/plates/${ev.target.dataset.id}`, { method:"DELETE" });
            await loadPlates();
          }catch(e){ alert("Erro: "+e.message); }
        });
        el.appendChild(row);
      }
    }
    document.getElementById("plate-form").addEventListener("submit", async (ev)=>{
      ev.preventDefault();
      const plate = document.getElementById("plate-input").value;
      const label = document.getElementById("label-input").value;
      const msg = document.getElementById("plate-msg");
      msg.textContent = "A adicionar...";
      try{
        const r = await fetch("/api/plates", {
          method:"POST",
          headers:{ "Content-Type":"application/json" },
          body: JSON.stringify({ plate, label }),
        });
        const data = await r.json();
        if(!r.ok) throw new Error(data.error || "erro");
        msg.textContent = `Adicionada: ${data.plate}`;
        document.getElementById("plate-input").value = "";
        document.getElementById("label-input").value = "";
        await loadPlates();
      }catch(e){ msg.textContent = "Erro: "+e.message; }
    });
    loadPlates();
  </script>
</body>
</html>
```

---

## 9. Ficheiros estáticos

### `static/style.css`

```bash
nano ~/anpr_web/static/style.css
```

```css
:root{
  --bg:#0f1115; --card:#171a21; --muted:#9aa4b2; --text:#eef2f7;
  --border:#273041; --ok:#22c55e; --bad:#ef4444; --warn:#f59e0b; --btn:#2563eb;
}
*{box-sizing:border-box}
body{margin:0;font-family:Arial,Helvetica,sans-serif;background:var(--bg);color:var(--text)}
.topbar{display:flex;justify-content:space-between;align-items:center;padding:18px 24px;border-bottom:1px solid var(--border)}
.layout{display:grid;grid-template-columns:2fr 1fr;gap:20px;padding:20px}
.left,.right{display:flex;flex-direction:column;gap:20px}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px}
.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
#live-feed{width:100%;border-radius:10px;background:#000;display:block}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.muted{color:var(--muted)}
.big{font-size:28px;font-weight:700;margin-top:8px}
.badge{background:rgba(34,197,94,.15);color:var(--ok);border:1px solid rgba(34,197,94,.3);padding:4px 10px;border-radius:999px;font-size:12px}
button,.linkbtn{border:none;background:var(--btn);color:white;padding:12px 16px;border-radius:10px;cursor:pointer;text-decoration:none;display:inline-block}
button.secondary,.linkbtn.secondary{background:#334155}
button.danger{width:100%;background:var(--bad);font-size:18px;font-weight:700}
.top-space{margin-top:12px}
.event{border-top:1px solid var(--border);padding:12px 0;display:grid;grid-template-columns:96px 1fr;gap:12px}
.event:first-child{border-top:none;padding-top:0}
.event img{width:96px;height:72px;object-fit:cover;border-radius:8px;background:#000}
.event-title{font-weight:700;margin-bottom:4px}
.ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)}
.login-wrap{min-height:100vh;display:grid;place-items:center}
.login-card{width:min(420px,90vw)}
.login-card input{width:100%;padding:12px;border-radius:10px;border:1px solid var(--border);background:#0d1016;color:#fff;margin:8px 0 12px}
.login-card button{width:100%}
.error{background:rgba(239,68,68,.15);border:1px solid rgba(239,68,68,.25);color:#fecaca;padding:10px;border-radius:8px;margin-bottom:10px}
.plate-form{display:grid;grid-template-columns:1fr 1fr auto;gap:8px}
.plate-form input{padding:10px;border-radius:8px;border:1px solid var(--border);background:#0d1016;color:#fff}
.plate-row{display:flex;justify-content:space-between;align-items:center;border-top:1px solid var(--border);padding:10px 0;gap:8px}
.plate-row:first-child{border-top:none}
.plate-row .info{flex:1}
.plate-row .plate{font-weight:700;font-family:Consolas,monospace}
.plate-row button{background:transparent;color:var(--bad);border:1px solid var(--bad);padding:6px 10px;font-size:13px}
@media (max-width:980px){.layout{grid-template-columns:1fr}.grid{grid-template-columns:1fr}}
@media (max-width:600px){.plate-form{grid-template-columns:1fr}}
```

### `static/app.js`

```bash
nano ~/anpr_web/static/app.js
```

```javascript
async function getJSON(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}
function fmtConfidence(v){ return v==null ? "--" : `${Math.round(v*100)}%`; }
function eventTitle(ev) {
  if (ev.event_type === "manual_open") return "Abertura manual";
  if (ev.event_type === "anpr_authorized") return "ANPR autorizado";
  if (ev.event_type === "anpr_denied") return "ANPR negado";
  if (ev.event_type === "plate_added") return "Matrícula adicionada";
  if (ev.event_type === "plate_removed") return "Matrícula removida";
  return ev.event_type;
}
function eventClass(ev) {
  if (ev.event_type === "manual_open") return "warn";
  if (ev.event_type === "plate_added") return "ok";
  if (ev.event_type === "plate_removed") return "warn";
  if (ev.authorized === 1) return "ok";
  if (ev.authorized === 0) return "bad";
  return "";
}
function renderEvents(events) {
  const el = document.getElementById("events");
  el.innerHTML = "";
  for (const ev of events) {
    const row = document.createElement("div");
    row.className = "event";
    const img = ev.snapshot_path
      ? `<img src="/${ev.snapshot_path}" alt="snapshot">`
      : `<div style="width:96px;height:72px;background:#000;border-radius:8px"></div>`;
    row.innerHTML = `
      ${img}
      <div>
        <div class="event-title ${eventClass(ev)}">${eventTitle(ev)}</div>
        <div class="muted">${ev.ts}</div>
        ${ev.plate ? `<div>Matrícula: <strong>${ev.plate}</strong></div>` : ""}
        ${ev.confidence !== null ? `<div>Confiança: ${fmtConfidence(ev.confidence)}</div>` : ""}
        ${ev.client_ip ? `<div>IP origem: ${ev.client_ip}</div>` : ""}
        ${ev.note ? `<div class="muted">${ev.note}</div>` : ""}
      </div>
    `;
    el.appendChild(row);
  }
}
async function refreshStatus() {
  const data = await getJSON("/api/status");
  document.getElementById("camera-ip").textContent = `Câmara: ${data.camera_ip}`;
  const latest = data.latest || {};
  document.getElementById("latest-plate").textContent = latest.plate || "--";
  document.getElementById("latest-confidence").textContent = fmtConfidence(latest.confidence);
  document.getElementById("latest-state").textContent =
    latest.authorized === true ? "Autorizada" :
    latest.authorized === false ? "Negada" : "--";
}
async function refreshEvents() {
  const events = await getJSON("/api/events?limit=4");
  renderEvents(events);
}
async function openGate() {
  const btn = document.getElementById("open-gate-btn");
  const result = document.getElementById("manual-result");
  btn.disabled = true; btn.textContent = "A abrir...";
  try {
    const data = await getJSON("/api/open_gate", { method: "POST" });
    result.textContent = `Portão aberto. IP origem: ${data.client_ip}`;
    await refreshEvents();
  } catch (e) {
    result.textContent = `Erro: ${e.message}`;
  } finally {
    btn.disabled = false; btn.textContent = "Abrir portão";
  }
}
document.getElementById("open-gate-btn")?.addEventListener("click", openGate);
async function tick() {
  try { await refreshStatus(); await refreshEvents(); }
  catch (e) { console.error(e); }
}
tick();
setInterval(tick, 3000);
```

---

## 10. Primeiro arranque (manual)

```bash
cd ~/anpr_web
source ~/anpr_env/bin/activate
python3 app.py
```

A primeira vez vai descarregar os modelos (`yolov8n.pt`, OCR, detetor de matrículas) — pode demorar 2–5 minutos.

Quando vires:
```
[ANPR] pronto
 * Running on http://0.0.0.0:8080
```

abre no browser de outro dispositivo da mesma rede:

```
http://IP_DO_PI:8080
```

Para descobrir o IP do Pi:
```bash
hostname -I
```

Password inicial: **`1234`** (muda no `app.py`).

---

## 11. Arranque automático (systemd)

```bash
sudo nano /etc/systemd/system/anpr-web.service
```

```ini
[Unit]
Description=ANPR Web UI
After=network-online.target
Wants=network-online.target

[Service]
User=desenvolvimento
WorkingDirectory=/home/desenvolvimento/anpr_web
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/desenvolvimento/anpr_env/bin/python /home/desenvolvimento/anpr_web/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Ativar:

```bash
sudo systemctl daemon-reload
sudo systemctl enable anpr-web.service
sudo systemctl start anpr-web.service
sudo systemctl status anpr-web.service
```

Ver logs em direto:
```bash
sudo journalctl -u anpr-web.service -f
```

---

## 12. IP fixo (recomendado)

```bash
sudo nmtui
```

→ "Edit a connection" → escolhe a tua ligação → "IPv4 Configuration: Manual"
→ Address, Gateway, DNS → OK → reinicia:

```bash
sudo systemctl restart NetworkManager
hostname -I
```

---

## 13. Backups

Faz backup destes ficheiros/pastas:

```bash
~/anpr_web/app.py
~/anpr_web/data/events.db
~/anpr_web/data/snapshots/
~/matriculas_validas.txt
```

Comando rápido:

```bash
tar czf ~/backup_anpr_$(date +%F).tar.gz \
    ~/anpr_web/app.py \
    ~/anpr_web/templates \
    ~/anpr_web/static \
    ~/anpr_web/data \
    ~/matriculas_validas.txt
```

---

## 14. Troubleshooting

| Sintoma | Solução |
|---------|---------|
| Página sem estilo | Verifica `~/anpr_web/static/style.css` existe; `Ctrl+Shift+R` no browser |
| `ModuleNotFoundError` | `source ~/anpr_env/bin/activate` e `pip install -r requirements.txt` |
| Câmara não aparece | Confirma RTSP no VLC primeiro: `vlc rtsp://user:pass@IP:554/stream1` |
| GPIO error: "Not running on RPi" | Instala `pip install rpi-lgpio` (substitui `RPi.GPIO` no Pi 5) |
| `inconsistent use of tabs and spaces` | `sed -i 's/\t/    /g' ~/anpr_web/app.py` |
| Erro 500 ao abrir portão | Confirma que `frame = stream_main.read()` não usa `or` com `stream_sub` |
| Snapshots não aparecem | Confirma rota `/data/snapshots/<path:filename>` no `app.py` |
| Memória cheia ao instalar | Aumenta swap (ver secção 6) |
| Service não arranca | `sudo journalctl -u anpr-web.service -n 100` para ver o erro |

---

## 15. Constantes principais (resumo)

No topo do `app.py`:

| Constante | O que faz | Default |
|-----------|-----------|---------|
| `WEB_PASSWORD` | Password de login | `"1234"` |
| `SECRET_KEY` | Cifra das sessões Flask | trocar! |
| `CAM_IP / USER / PASS` | Câmara IP | — |
| `RELE_PIN` | Pino BCM do relé | `17` |
| `TEMPO_RELE_SEG` | Quanto tempo aciona o relé | `2.0` |
| `ANPR_MIN_CONF_SAVE` | Confiança mínima para guardar/abrir | `0.95` |
| `ANPR_COOLDOWN_S` | Anti-repetição da mesma matrícula | `10` |
| `DETECTAR_CADA_N` | Processa 1 em cada N frames | `5` |
| `APP_PORT` | Porta do servidor web | `8080` |

---

## 16. Endpoints da API

| Método | URL | Função |
|--------|-----|--------|
| GET  | `/` | Dashboard |
| GET  | `/history` | Histórico completo |
| GET  | `/plates` | Gestão de matrículas |
| GET  | `/video_feed` | Stream MJPEG |
| GET  | `/api/status` | Estado atual + última leitura |
| GET  | `/api/events?limit=N` | Lista eventos |
| GET  | `/api/events/all?limit=N` | Lista eventos (histórico) |
| POST | `/api/open_gate` | Abre portão manualmente |
| GET  | `/api/plates` | Lista matrículas autorizadas |
| POST | `/api/plates` | Adiciona matrícula |
| DELETE | `/api/plates/<id>` | Remove matrícula |
| POST | `/api/reload_plates` | Recarrega lista do ficheiro |
| GET  | `/health` | Health-check (sem auth) |

---

## 17. Próximos passos opcionais

- HTTPS com Nginx + Let's Encrypt
- Autenticação por utilizador/password (múltiplos users)
- Notificações por Telegram quando matrícula desconhecida
- Export CSV do histórico
- App móvel (PWA)
- Backup automático para NAS/cloud

---

**Bom uso! 🚗🟢**