import os
import re
import cv2
import time
import json
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, Response, render_template, request, jsonify, redirect, url_for, session

# ===================== CONFIG =====================
APP_HOST = "0.0.0.0"
APP_PORT = 8080
SECRET_KEY = "troca_esta_chave_agora"
WEB_PASSWORD = "1234"   # troca isto

CAM_IP   = "191.188.127.7"
CAM_USER = "admin"
CAM_PASS = "desenvolvimento1986"
CAM_PORT = 554

RTSP_MAIN = f"rtsp://{CAM_USER}:{CAM_PASS}@{CAM_IP}:{CAM_PORT}/stream1"
RTSP_SUB  = f"rtsp://{CAM_USER}:{CAM_PASS}@{CAM_IP}:{CAM_PORT}/stream2"
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"

RELE_PIN         = 17
RELE_ATIVO_BAIXO = False
TEMPO_RELE_SEG   = 2.0

BASE_DIR = Path.home() / "anpr_web"
DATA_DIR = BASE_DIR / "data"
SNAP_DIR = DATA_DIR / "snapshots"
DB_PATH  = DATA_DIR / "events.db"
EVENTS_LOG_PATH = DATA_DIR / "events.log"
VALIDAS_PATH = Path.home() / "matriculas_validas.txt"
MAX_EVENTS_HISTORY = 500

for p in [DATA_DIR, SNAP_DIR]:
    p.mkdir(parents=True, exist_ok=True)

# ===================== OPCIONAL GPIO =====================
try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    GPIO_AVAILABLE = False

# ===================== ANPR OPCIONAL =====================
ANPR_ENABLED = True
try:
    import numpy as np
    from ultralytics import YOLO
    from fast_plate_ocr import LicensePlateRecognizer
    from open_image_models import LicensePlateDetector
except Exception as e:
    print(f"[WARN] ANPR libs não disponíveis: {e}")
    ANPR_ENABLED = False

MODEL_VEHICLE   = "yolov8n.pt"
VEHICLE_CLASSES = {2, 3, 5, 7}
CONF_VEHICLE    = 0.45
CONF_PLATE      = 0.25
OCR_MODEL       = "global-plates-mobile-vit-v2-model"
YOLO_IMGSZ      = 416
DETECTAR_CADA_N = 5
ANPR_CHECK_INTERVAL_S = 10
ANPR_PLATE_RECHECK_S = 300
ANPR_MIN_CONF_SAVE = 0.95
RELE_COOLDOWN_S = 180

REGEX_MATRICULA = re.compile(
    r"^("
    r"[A-Z]{2}\d{2}\d{2}|"
    r"\d{2}\d{2}[A-Z]{2}|"
    r"\d{2}[A-Z]{2}\d{2}|"
    r"[A-Z]{2}\d{2}[A-Z]{2}|"
    r"\d{4}[A-Z]{3}"
    r")$"
)

REGEX_MATRICULA_EUROPA_GENERICO = re.compile(
    r"^(?=.{5,10}$)(?=.*[A-Z])(?=.*\d)[A-Z0-9]+$"
)

# ===================== APP =====================
app = Flask(__name__)
app.secret_key = SECRET_KEY


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def normalize_plate(value):
    return re.sub(r"[^A-Z0-9]", "", str(value).upper())


def is_european_plate_format(plate):
    p = normalize_plate(plate)
    # Mantem os formatos conhecidos e aceita o padrao europeu generico.
    return bool(REGEX_MATRICULA.match(p) or REGEX_MATRICULA_EUROPA_GENERICO.match(p))


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
    # Migracao: novas colunas opcionais (validade, horario, dias, creditos).
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(plates)").fetchall()}
    additions = [
        ("expires_at", "TEXT"),
        ("time_start", "TEXT"),
        ("time_end", "TEXT"),
        ("days_mask", "INTEGER"),
        ("max_uses", "INTEGER"),
        ("uses_count", "INTEGER NOT NULL DEFAULT 0"),
        ("is_external_bus", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for name, ddl in additions:
        if name not in existing_cols:
            conn.execute(f"ALTER TABLE plates ADD COLUMN {name} {ddl}")
    conn.commit()
    conn.close()

def add_event(event_type, plate=None, confidence=None, authorized=None,
              client_ip_value=None, user_agent=None, snapshot_path=None, note=None):
    event_record = {
        "ts": now_iso(),
        "event_type": event_type,
        "plate": plate,
        "confidence": confidence,
        "authorized": None if authorized is None else (1 if authorized else 0),
        "client_ip": client_ip_value,
        "user_agent": user_agent,
        "snapshot_path": snapshot_path,
        "note": note,
    }

    # Mantem um log completo (append-only), independente da rotacao da BD.
    try:
        with EVENTS_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event_record) + "\n")
    except Exception as e:
        print(f"[events.log] erro ao gravar: {e}")

    conn = db()
    conn.execute("""
        INSERT INTO events (ts, event_type, plate, confidence, authorized, client_ip, user_agent, snapshot_path, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        event_record["ts"],
        event_record["event_type"],
        event_record["plate"],
        event_record["confidence"],
        event_record["authorized"],
        event_record["client_ip"],
        event_record["user_agent"],
        event_record["snapshot_path"],
        event_record["note"]
    ))
    conn.execute("""
        DELETE FROM events
        WHERE id NOT IN (
            SELECT id FROM events
            ORDER BY id DESC
            LIMIT ?
        )
    """, (MAX_EVENTS_HISTORY,))
    conn.commit()
    conn.close()


def list_events(limit=20):
    conn = db()
    rows = conn.execute("""
        SELECT * FROM events
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def purge_expired_plates():
    """Apaga matriculas cuja validade ja terminou ou cujos creditos esgotaram."""
    try:
        conn = db()
        now = now_iso()
        # Por data de expiracao
        conn.execute("DELETE FROM plates WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
        # Por creditos esgotados
        conn.execute("DELETE FROM plates WHERE max_uses IS NOT NULL AND uses_count >= max_uses")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[plates purge] erro: {e}")


def load_valid_plates():
    """Retorna o conjunto de matriculas registadas (ativas), apos limpar expiradas."""
    purge_expired_plates()
    plates = set()
    # 1) Ficheiro de texto (compat)
    if VALIDAS_PATH.exists():
        for line in VALIDAS_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                plates.add(normalize_plate(line))
    else:
        VALIDAS_PATH.write_text("# uma matricula por linha\n", encoding="utf-8")
    # 2) Base de dados
    try:
        conn = db()
        rows = conn.execute("SELECT plate FROM plates WHERE active=1").fetchall()
        conn.close()
        for r in rows:
            plates.add(normalize_plate(r["plate"]))
    except Exception as e:
        print(f"[plates] erro: {e}")
    return plates


def _time_in_window(now_hm, start_hm, end_hm):
    """Verifica se HH:MM esta dentro da janela. Suporta passagem por meia-noite."""
    if not start_hm or not end_hm:
        return True
    if start_hm <= end_hm:
        return start_hm <= now_hm <= end_hm
    # janela que atravessa a meia-noite (ex: 22:00 - 06:00)
    return now_hm >= start_hm or now_hm <= end_hm


def check_plate_authorization(plate_key):
    """
    Verifica se a matricula esta autorizada a abrir o portao neste momento.
    Retorna (registered: bool, authorized: bool, reason: str|None).
    Efeitos colaterais:
      - Apaga matricula se expirou por data.
      - Incrementa contador de uso quando autoriza; apaga ao esgotar creditos.
    """
    plate_key = normalize_plate(plate_key)
    if not plate_key:
        return False, False, "vazia"

    # 1) Compat: ficheiro de texto - sempre autoriza sem restricoes
    if VALIDAS_PATH.exists():
        for line in VALIDAS_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and normalize_plate(line) == plate_key:
                return True, True, None

    conn = db()
    row = conn.execute("SELECT * FROM plates WHERE plate=?", (plate_key,)).fetchone()
    if not row:
        conn.close()
        return False, False, "nao registada"

    row = dict(row)
    now = datetime.now()
    now_iso_str = now.isoformat(timespec="seconds")

    # Expirada por data -> apaga e bloqueia
    if row.get("expires_at") and row["expires_at"] <= now_iso_str:
        conn.execute("DELETE FROM plates WHERE id=?", (row["id"],))
        conn.commit()
        conn.close()
        return False, False, "expirada"

    if not row.get("active"):
        conn.close()
        return True, False, "inativa"

    # Dia da semana (bit 0 = Segunda ... bit 6 = Domingo)
    days_mask = row.get("days_mask")
    if days_mask is not None and days_mask != 0:
        weekday = now.weekday()
        if not (int(days_mask) & (1 << weekday)):
            conn.close()
            return True, False, "dia nao permitido"

    # Janela horaria
    now_hm = now.strftime("%H:%M")
    if not _time_in_window(now_hm, row.get("time_start"), row.get("time_end")):
        conn.close()
        return True, False, "fora de horario"

    # Creditos
    max_uses = row.get("max_uses")
    uses_count = int(row.get("uses_count") or 0)
    if max_uses is not None and uses_count >= int(max_uses):
        conn.execute("DELETE FROM plates WHERE id=?", (row["id"],))
        conn.commit()
        conn.close()
        return False, False, "creditos esgotados"

    # Autorizado: incrementa contador; se atingir o limite, apaga.
    new_count = uses_count + 1
    if max_uses is not None and new_count >= int(max_uses):
        conn.execute("DELETE FROM plates WHERE id=?", (row["id"],))
    else:
        conn.execute("UPDATE plates SET uses_count=? WHERE id=?", (new_count, row["id"]))
    conn.commit()
    conn.close()
    return True, True, None

class GPIOController:
    def __init__(self, pin, ativo_baixo=True):
        self.pin = pin
        self.ativo_baixo = ativo_baixo
        if GPIO_AVAILABLE:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            try:
                GPIO.cleanup(self.pin)
            except Exception:
                pass
            GPIO.setup(self.pin, GPIO.OUT,
                       initial=GPIO.HIGH if ativo_baixo else GPIO.LOW)

    def acionar(self, segundos):
        if GPIO_AVAILABLE:
            GPIO.output(self.pin, GPIO.LOW if self.ativo_baixo else GPIO.HIGH)
            time.sleep(segundos)
            GPIO.output(self.pin, GPIO.HIGH if self.ativo_baixo else GPIO.LOW)
        else:
            time.sleep(segundos)

    def cleanup(self):
        if GPIO_AVAILABLE:
            GPIO.cleanup()


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
                    print(f"[RTSP {self.nome}] reconectar")
                    try:
                        self.cap.release()
                    except Exception:
                        pass
                    self.cap = None
                    falhas = 0
                continue
            falhas = 0
            with self.lock:
                self.frame = frame

    def read(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()

    def stop(self):
        self.running = False
        try:
            if self.cap:
                self.cap.release()
        except Exception:
            pass


stream_sub = RTSPStream(RTSP_SUB, "sub").start()
stream_main = RTSPStream(RTSP_MAIN, "main").start()
gpio = GPIOController(RELE_PIN, RELE_ATIVO_BAIXO)

anpr_lock = threading.Lock()
anpr_latest = {
    "plate": None,
    "confidence": None,
    "authorized": None,
    "ts": None
}
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
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" +
            jpg.tobytes() +
            b"\r\n"
        )
        time.sleep(0.05)


# ===================== ANPR =====================
class ANPREngine:
    def __init__(self):
        self.enabled = ANPR_ENABLED
        self.running = False
        self.processando = False
        self.frame_count = 0
        self.last_boxes = []
        self.ultimo_snapshot_por_matricula = {}
        self.ultimo_rele_ts = 0
        self.ultimo_check_ts = 0

        if not self.enabled:
            print("[ANPR] desativado")
            return

        print("[ANPR] a carregar modelos...")
        self.model_vehicle = YOLO(MODEL_VEHICLE)
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
            plate_img = cv2.resize(
                plate_img,
                (target_w, max(32, int(ph * s))),
                interpolation=cv2.INTER_CUBIC
            )

        plate_gray = cv2.cvtColor(plate_img, cv2.COLOR_BGR2GRAY)
        resultado = self.ocr.run(plate_gray, return_confidence=True)

        preds = resultado if isinstance(resultado, list) else [resultado]
        candidatos = []

        for pred in preds:
            if hasattr(pred, "plate"):
                texto = pred.plate
                try:
                    conf = float(np.mean(pred.char_probs))
                except Exception:
                    conf = 0.5
            elif isinstance(pred, tuple) and len(pred) == 2:
                texto, confs = pred
                try:
                    conf = float(np.mean(confs))
                except Exception:
                    conf = 0.5
            else:
                texto = str(pred)
                conf = 0.5

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
            detections = self.plate_detector.predict(roi)
            for det in detections:
                if float(det.confidence) < CONF_PLATE:
                    continue
                bb = det.bounding_box
                px1, py1, px2, py2 = int(bb.x1), int(bb.y1), int(bb.x2), int(bb.y2)
                px1 = max(0, px1 - 6); py1 = max(0, py1 - 6)
                px2 = min(roi.shape[1], px2 + 6); py2 = min(roi.shape[0], py2 + 6)
                plate_img = roi[py1:py2, px1:px2]
                if plate_img.size == 0:
                    continue

                for texto, conf, processed_img in self._ocr_candidates(plate_img):
                    candidatos.append((texto, conf))
                    if best_crop is None:
                        best_crop = processed_img

        candidatos = [(t, c) for (t, c) in candidatos if c >= 0.70]
        candidatos.sort(key=lambda x: x[1], reverse=True)

        for txt, c in candidatos:
            if is_european_plate_format(txt):
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
            plate_key = normalize_plate(plate)
            plate_in_eu_format = is_european_plate_format(plate_key)
            registered, authorized, reason = check_plate_authorization(plate_key)

            if not plate_in_eu_format and not registered:
                print(f"[ANPR] ignorado {plate_key} (personalizada nao registada)")
                return

            if (agora - self.ultimo_snapshot_por_matricula.get(plate_key, 0)) < ANPR_PLATE_RECHECK_S:
                return

            self.ultimo_snapshot_por_matricula[plate_key] = agora
            snap = save_snapshot(frame, "anpr")

            relay_acionado = False
            note = "Reconhecimento ANPR"
            if registered and not authorized and reason:
                note = f"Reconhecimento ANPR (bloqueado: {reason})"
            elif not plate_in_eu_format and authorized:
                note = "Reconhecimento ANPR (matricula personalizada autorizada)"

            if authorized:
                if (agora - self.ultimo_rele_ts) >= RELE_COOLDOWN_S:
                    open_gate()
                    self.ultimo_rele_ts = agora
                    relay_acionado = True
                else:
                    restante = int(RELE_COOLDOWN_S - (agora - self.ultimo_rele_ts))
                    note = f"Reconhecimento ANPR (rele em cooldown, faltam {max(0, restante)}s)"

            add_event(
                event_type="anpr_authorized" if authorized else "anpr_denied",
                plate=plate,
                confidence=conf,
                authorized=authorized,
                snapshot_path=snap,
                note=note if not relay_acionado else "Reconhecimento ANPR (rele acionado)"
            )

            anpr_latest.update({
                "plate": plate,
                "confidence": conf,
                "authorized": authorized,
                "ts": now_iso()
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
                    time.sleep(0.1)
                    continue
                self.frame_count += 1
                if self.frame_count % DETECTAR_CADA_N != 0:
                    time.sleep(0.01)
                    continue

                agora = time.time()
                if (agora - self.ultimo_check_ts) < ANPR_CHECK_INTERVAL_S:
                    time.sleep(0.05)
                    continue

                res = self.model_vehicle(frame, imgsz=YOLO_IMGSZ, conf=CONF_VEHICLE, verbose=False)[0]
                veiculo = any(int(box.cls[0]) in VEHICLE_CLASSES for box in res.boxes)

                if veiculo and not self.processando:
                    self.ultimo_check_ts = agora
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

@app.route("/api/events/all")
@login_required
def api_events_all():
    limit = int(request.args.get("limit", 200))
    return jsonify(list_events(limit))


@app.route("/video_feed")
@login_required
def video_feed():
    return Response(mjpeg_generator(), mimetype="multipart/x-mixed-replace; boundary=frame")


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
        event_type="manual_open",
        authorized=True,
        client_ip_value=ip,
        user_agent=ua,
        snapshot_path=snap,
        note="Abertura manual via UI web"
    )

    return jsonify({"ok": True, "client_ip": ip, "snapshot": snap})

def _parse_days(value):
    """Aceita lista de inteiros 0..6 (Mon..Sun) ou int bitmask. Retorna bitmask ou None."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value & 127
    if isinstance(value, list):
        mask = 0
        for d in value:
            try:
                di = int(d)
            except (TypeError, ValueError):
                continue
            if 0 <= di <= 6:
                mask |= (1 << di)
        return mask if mask else None
    return None


def _parse_hhmm(value):
    if not value:
        return None
    s = str(value).strip()
    if re.match(r"^\d{1,2}:\d{2}$", s):
        h, m = s.split(":")
        return f"{int(h):02d}:{int(m):02d}"
    return None


def _compute_expires_at(days_valid, explicit):
    if explicit:
        return str(explicit).strip()
    try:
        n = int(days_valid)
        if n > 0:
            return (datetime.now() + timedelta(days=n)).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None
    return None


def _plate_row_dict(row):
    d = dict(row)
    if d.get("days_mask") is not None:
        m = int(d["days_mask"])
        d["days"] = [i for i in range(7) if m & (1 << i)]
    else:
        d["days"] = None
    return d


@app.route("/api/plates", methods=["GET"])
@login_required
def api_plates_list():
    purge_expired_plates()
    conn = db()
    rows = conn.execute("""
        SELECT id, plate, label, added_at, added_by_ip, active,
               expires_at, time_start, time_end, days_mask, max_uses, uses_count,
               is_external_bus
        FROM plates ORDER BY id DESC
    """).fetchall()
    conn.close()
    return jsonify([_plate_row_dict(r) for r in rows])


@app.route("/api/plates", methods=["POST"])
@login_required
def api_plates_add():
    global valid_plates
    data = request.get_json(silent=True) or request.form

    # Aceita 'plate' (string) ou 'plates' (string com varias linhas / lista).
    raw_plates = data.get("plates")
    if raw_plates is None:
        raw_plates = data.get("plate", "")

    if isinstance(raw_plates, list):
        candidates = raw_plates
    else:
        candidates = re.split(r"[\s,;]+", str(raw_plates))

    cleaned = []
    for c in candidates:
        p = re.sub(r"[^A-Z0-9]", "", str(c).upper())
        if 4 <= len(p) <= 9:
            cleaned.append(p)
    cleaned = list(dict.fromkeys(cleaned))  # preserva ordem, remove duplicados

    if not cleaned:
        return jsonify({"ok": False, "error": "Nenhuma matricula valida"}), 400

    label = (data.get("label") or "").strip() or None
    expires_at = _compute_expires_at(data.get("days_valid"), data.get("expires_at"))
    time_start = _parse_hhmm(data.get("time_start"))
    time_end = _parse_hhmm(data.get("time_end"))
    days_mask = _parse_days(data.get("days"))
    is_external_bus = 1 if bool(data.get("is_external_bus")) else 0
    try:
        max_uses = int(data.get("max_uses")) if data.get("max_uses") not in (None, "", 0, "0") else None
        if max_uses is not None and max_uses <= 0:
            max_uses = None
    except (TypeError, ValueError):
        max_uses = None

    # Autocarro externo: forca abertura unica.
    if is_external_bus:
        max_uses = 1

    ip = client_ip()
    ua = request.headers.get("User-Agent", "")

    added = []
    skipped = []
    conn = db()
    for plate in cleaned:
        try:
            conn.execute("""
                INSERT INTO plates (plate, label, added_at, added_by_ip, added_by_ua, active,
                                    expires_at, time_start, time_end, days_mask, max_uses, uses_count,
                                    is_external_bus)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 0, ?)
            """, (plate, label, now_iso(), ip, ua,
                  expires_at, time_start, time_end, days_mask, max_uses, is_external_bus))
            added.append(plate)
        except sqlite3.IntegrityError:
            skipped.append(plate)
    conn.commit()
    conn.close()

    valid_plates = load_valid_plates()

    if added:
        add_event(
            event_type="plate_added",
            plate=", ".join(added),
            authorized=True,
            client_ip_value=ip,
            user_agent=ua,
            note=f"Matriculas adicionadas ({len(added)}){(' - ' + label) if label else ''}"
        )

    return jsonify({"ok": True, "added": added, "skipped": skipped})


@app.route("/api/plates/<int:pid>", methods=["PATCH"])
@login_required
def api_plates_update(pid):
    global valid_plates
    data = request.get_json(silent=True) or {}

    conn = db()
    row = conn.execute("SELECT * FROM plates WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "Nao encontrada"}), 404

    fields = []
    values = []

    if "label" in data:
        fields.append("label=?")
        values.append((data.get("label") or "").strip() or None)

    if "active" in data:
        fields.append("active=?")
        values.append(1 if bool(data["active"]) else 0)

    if "expires_at" in data or "days_valid" in data:
        fields.append("expires_at=?")
        values.append(_compute_expires_at(data.get("days_valid"), data.get("expires_at")))

    if "time_start" in data:
        fields.append("time_start=?")
        values.append(_parse_hhmm(data.get("time_start")))

    if "time_end" in data:
        fields.append("time_end=?")
        values.append(_parse_hhmm(data.get("time_end")))

    if "days" in data:
        fields.append("days_mask=?")
        values.append(_parse_days(data.get("days")))

    if "max_uses" in data:
        try:
            mv = int(data["max_uses"]) if data["max_uses"] not in (None, "", 0, "0") else None
            if mv is not None and mv <= 0:
                mv = None
        except (TypeError, ValueError):
            mv = None
        fields.append("max_uses=?")
        values.append(mv)

    if "is_external_bus" in data:
        ext = 1 if bool(data["is_external_bus"]) else 0
        fields.append("is_external_bus=?")
        values.append(ext)
        if ext:
            # Garante abertura unica.
            fields.append("max_uses=?")
            values.append(1)

    if "reset_uses" in data and data["reset_uses"]:
        fields.append("uses_count=?")
        values.append(0)

    if not fields:
        conn.close()
        return jsonify({"ok": False, "error": "Nada para atualizar"}), 400

    values.append(pid)
    conn.execute(f"UPDATE plates SET {', '.join(fields)} WHERE id=?", values)
    conn.commit()
    conn.close()

    valid_plates = load_valid_plates()
    return jsonify({"ok": True})


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
        return jsonify({"ok": False, "error": "Nao encontrada"}), 404
    plate = row["plate"]
    conn.execute("DELETE FROM plates WHERE id=?", (pid,))
    conn.commit()
    conn.close()

    valid_plates = load_valid_plates()

    add_event(
        event_type="plate_removed",
        plate=plate,
        authorized=False,
        client_ip_value=ip,
        user_agent=ua,
        note="Matricula removida"
    )
    return jsonify({"ok": True})

@app.route("/api/reload_plates", methods=["POST"])
@login_required
def api_reload_plates():
    global valid_plates
    valid_plates = load_valid_plates()
    return jsonify({"ok": True, "count": len(valid_plates)})

from flask import send_from_directory

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
