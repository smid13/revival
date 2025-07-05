
from flask import Flask, request, redirect, render_template, jsonify, url_for, abort, send_file, flash
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Time, cast, Integer
from sqlalchemy.orm import joinedload
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import qrcode
import io
import os
import pytz
from dotenv import load_dotenv
from supabase_upload import upload_qr_to_supabase
import sqlalchemy as sa
import requests
from bs4 import BeautifulSoup
import pandas as pd
import unicodedata
import re
from werkzeug.utils import secure_filename
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from openpyxl.utils import get_column_letter
import math

# Flask a SQLAlchemy setup
app = Flask(__name__)
db_url = os.getenv("SQLALCHEMY_DATABASE_URI")
if not db_url:
    raise RuntimeError("Chybí proměnná SQLALCHEMY_DATABASE_URI")
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
db = SQLAlchemy(app)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")

def get_czech_time():
    return datetime.now(ZoneInfo("Europe/Prague"))
    
# Modely
class Race(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    start_time = db.Column(db.DateTime)
    crew_interval = db.Column(db.Integer)  # v minutách

    crews = db.relationship('Crew', backref='race', lazy=True)
    checkpoints = db.relationship('Checkpoint', backref='race', lazy=True)

class Crew(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(10))
    name = db.Column(db.String(100))
    vehicle = db.Column(db.String(100))
    race_id = db.Column(db.Integer, db.ForeignKey('race.id'), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    qr_code_url = db.Column(db.String(500))
    category = db.Column(db.String(50), nullable=True)  # Třída závodu
    vehicle_year = db.Column(db.String(50), nullable=True)  # Rok výroby
    penalty_year = db.Column(db.String(50), default=0)  # Trestné body za rok výroby

class Checkpoint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    order = db.Column(db.Integer)
    race_id = db.Column(db.Integer, db.ForeignKey('race.id'), nullable=False)

class ScanRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    crew_id = db.Column(db.Integer, db.ForeignKey('crew.id'), nullable=False)
    checkpoint_id = db.Column(db.Integer, db.ForeignKey('checkpoint.id'), nullable=False)
    timestamp = db.Column(db.DateTime(timezone=True), default=get_czech_time)

    crew = db.relationship('Crew', backref='scans')
    checkpoint = db.relationship('Checkpoint', backref='scans')

class IdealTime(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    crew_id = db.Column(db.Integer, db.ForeignKey('crew.id'), nullable=False)
    checkpoint_id = db.Column(db.Integer, db.ForeignKey('checkpoint.id'), nullable=False)
    ideal_time = db.Column(Time)

    crew = db.relationship('Crew', backref='ideal_times')
    checkpoint = db.relationship('Checkpoint', backref='ideal_times')

with app.app_context():
    db.create_all()

def safe_int(val):
    try:
        if str(val).strip().lower() in ["", "nan", "none"]:
            return 0
        return int(float(val))
    except (ValueError, TypeError):
        return 0

def generate_qr_with_center_text(data: str, center_text: str) -> io.BytesIO:

    # Vytvoření QR kódu
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    draw = ImageDraw.Draw(qr_img)
    width, height = qr_img.size

    # Bílý čtverec ve středu – cca 20 % velikosti QR obrázku
    box_size = int(width * 0.25)
    top_left = ((width - box_size) // 2, (height - box_size) // 2)
    bottom_right = ((width + box_size) // 2, (height + box_size) // 2)
    draw.rectangle([top_left, bottom_right], fill="white")

    # Dynamické přizpůsobení velikosti písma
    box_width = bottom_right[0] - top_left[0]
    box_height = bottom_right[1] - top_left[1]
    max_font_size = box_height  # Začneme největší možnou velikostí
    font = None

    for size in range(max_font_size, 0, -1):
        try:
            candidate_font = ImageFont.truetype("DejaVuSans.ttf", size=size)
        except IOError:
            candidate_font = ImageFont.load_default()
        
        text_bbox = draw.textbbox((0, 0), center_text, font=candidate_font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]

        if text_width <= box_width and text_height <= box_height:
            font = candidate_font
            break

    if font is None:
        font = ImageFont.load_default()
        print("⚠️ Nepodařilo se najít vhodnou velikost písma, použit fallback font.")
    else:
        print(font)
    # Zarovnání textu doprostřed bílého rámečku
    text_bbox = draw.textbbox((0, 0), center_text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    text_x = top_left[0] + (box_width - text_width) // 2
    text_y = top_left[1] + (box_height - text_height) // 2
    draw.text((text_x, text_y), center_text, font=font, fill="black")

    # Výstup jako PNG do paměti
    buffer = io.BytesIO()
    qr_img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer





def recalculate_all_ideal_times(race_id):
    race = Race.query.get(race_id)
    if not race:
        return

    checkpoints = Checkpoint.query.filter_by(race_id=race.id).order_by(Checkpoint.order).all()

    # Vezmeme všechny posádky závodu
    all_crews = Crew.query.filter_by(race_id=race.id).all()

    # Seřadíme aktivní posádky podle čísla
    active_crews = sorted(
        [c for c in all_crews if c.is_active and c.number.isdigit()],
        key=lambda x: int(x.number)
    )

    # Najdeme první aktivní posádku jako referenci
    reference_crew = active_crews[0] if active_crews else None
    if not reference_crew:
        return  # není žádná aktivní posádka, neděláme nic

    # Načteme ideální časy referenční posádky
    base_times = IdealTime.query.filter(
        IdealTime.crew_id == reference_crew.id,
        IdealTime.checkpoint_id.in_([ck.id for ck in checkpoints])
    ).order_by(IdealTime.checkpoint_id).all()

    if not base_times:
        return  # není co přepočítávat

    # Vymažeme všechny ideální časy tohoto závodu
    crew_ids = [c.id for c in all_crews]
    IdealTime.query.filter(IdealTime.crew_id.in_(crew_ids)).delete()
    db.session.commit()

    # Výpočet nových časů
    base_date = race.start_time.date() if race.start_time else datetime.today().date()
    for idx, crew in enumerate(active_crews):  # idx = pozice v aktivních posádkách
        for bt in base_times:
            orig_dt = datetime.combine(base_date, bt.ideal_time)
            new_dt = orig_dt + timedelta(minutes=idx * race.crew_interval)

            new_ideal = IdealTime(
                crew_id=crew.id,
                checkpoint_id=bt.checkpoint_id,
                ideal_time=new_dt.time()
            )
            db.session.add(new_ideal)

    db.session.commit()

def sanitize_filename(name):
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = re.sub(r"[^\w\-]+", "_", name)
    return name.strip("_")

@app.route("/race/<int:race_id>/import_crews", methods=["POST"])
def import_crews(race_id):
    race = Race.query.get_or_404(race_id)
    source_url = request.form.get("source_url")
    start_row = int(request.form.get("start_row",1))
    if not source_url:
        return "Chybí source_url", 400

    try:
        tables = pd.read_html(source_url)
    except Exception as e:
        return f"Nepodařilo se načíst tabulku ze stránky: {e}", 500

    if not tables:
        return "Nebyly nalezeny žádné tabulky", 400

    # Vybereme první tabulku (nebo podle obsahu i jinou)
    df = tables[0]
    df = df.iloc[start_row:]
    # Debug: zobraz strukturu tabulky
    print(df.head())

    # Předpokládejme, že sloupce jsou např.: "Číslo", "Jméno", "Vozidlo"
    required_columns = [0, 1, 2, 5, 6, 7]
    if not all(col in df.columns for col in required_columns):
        return f"Chybí očekávané sloupce. Nalezeno: {df.columns}", 400

    new_crews = []
    for _, row in df.iterrows():
        number = str(row[0]).strip()
        name = str(row[1]).strip()
        vehicle = str(row[2]).strip()
        penalty_year = str(row[5]).strip()
        vehicle_year = str(row[6]).strip()
        category = str(row[7]).strip()

        crew = Crew(
            number=number,
            name=name,
            vehicle=vehicle,
            category=category,
            vehicle_year=vehicle_year,
            penalty_year=penalty_year,
            race_id=race.id
        )
        db.session.add(crew)
        new_crews.append(crew)

    db.session.commit()

    # QR kódy + Supabase upload
    for crew in new_crews:
        qr_buffer = generate_qr_with_center_text(str(crew.id), crew.number)
        filename = secure_filename(f"{crew.name}_{crew.id}.png")
        tmp_path = f"/tmp/{filename}"
        with open(tmp_path, "wb") as f:
            f.write(qr_buffer.getvalue())

        public_url = upload_qr_to_supabase(tmp_path, filename)
        crew.qr_code_url = public_url

    db.session.commit()

    recalculate_all_ideal_times(race.id)

    return f"Importováno {len(new_crews)} posádek z tabulky.", 200



@app.route("/")
def index():
    races = Race.query.order_by(Race.start_time.desc()).all()
    return render_template("index.html", races=races)

#smazaní databáze
from flask import render_template, request, redirect, url_for, flash

@app.route('/delete-database', methods=['GET', 'POST'])
def delete_database():
    if request.method == 'POST':
        try:
            db.session.close()
            db.drop_all()
            db.create_all()
            db.session.commit()
            flash('Databáze byla úspěšně smazána a znovu vytvořena', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Chyba při mazání databáze: {str(e)}', 'danger')
        return redirect(url_for('index'))

    # GET požadavek zobrazí potvrzovací stránku
    return render_template('confirm_delete.html')

    
@app.route("/create_race", methods=["GET", "POST"])
def create_race():
    if request.method == "POST":
        name = request.form["name"]
        start_time = datetime.strptime(request.form["start_time"], "%Y-%m-%d %H:%M")
        checkpoint_count = int(request.form["checkpoint_count"])
        interval = int(request.form["interval"])

        race = Race(name=name, start_time=start_time, crew_interval=interval)
        db.session.add(race)
        db.session.commit()

        # Vytvoření CK
        for i in range(checkpoint_count):
            ck = Checkpoint(name=f"CK {i+1}", order=i+1, race_id=race.id)
            db.session.add(ck)

        db.session.commit()
        return redirect(f"/race/{race.id}")
    return render_template("create_race.html")

@app.route("/race/<int:race_id>")
def race_detail(race_id):
    race = Race.query.get_or_404(race_id)
    crews = Crew.query.filter_by(race_id=race.id).order_by(cast(Crew.number, Integer)).all()
    checkpoints = Checkpoint.query.filter_by(race_id=race.id).order_by(Checkpoint.order).all()
    
    # Získání ideálních časů první posádky
    first_crew = next((crew for crew in crews if crew.number == '1'), None)
    checkpoint_times = {}
    if first_crew:
        ideal_times = IdealTime.query.filter_by(crew_id=first_crew.id).all()
        checkpoint_times = {time.checkpoint_id: time.ideal_time.strftime('%H:%M') for time in ideal_times}

    # Ideální časy
    all_ideal_times = IdealTime.query.join(Crew).filter(Crew.race_id == race.id).all()
    crew_times = {}

    for time in all_ideal_times:
        crew_times.setdefault(time.crew_id, {})[time.checkpoint_id] = time.ideal_time.strftime('%H:%M')
        
    # REÁLNÉ ČASY
    all_scans = ScanRecord.query.join(Crew).filter(Crew.race_id == race.id).all()
    scan_times = {}
    for scan in all_scans:
        scan_times.setdefault(scan.crew_id, {})[scan.checkpoint_id] = scan.timestamp.strftime('%H:%M')
        
    return render_template(
        "race_detail.html", 
        race=race, 
        crews=crews, 
        checkpoints=checkpoints,
        checkpoint_times=checkpoint_times,
        crew_times=crew_times,
        scan_times=scan_times
    )

@app.route("/race/<int:race_id>/crews", methods=["GET"])
def manage_crews(race_id):
    race = Race.query.get_or_404(race_id)
    crews = Crew.query.filter_by(race_id=race.id).order_by(cast(Crew.number, Integer)).all()
    return render_template("manage_crews.html", race=race, crews=crews)

@app.route("/race/<int:race_id>/crews/create", methods=["GET", "POST"])
def create_crew(race_id):
    race = Race.query.get_or_404(race_id)
    checkpoints = Checkpoint.query.filter_by(race_id=race_id).order_by(Checkpoint.order).all()
    
    if request.method == "POST":
        name = request.form["name"]
        vehicle = request.form["vehicle"]
        number = request.form["number"]

        crew = Crew(name=name, vehicle=vehicle, number=number, race_id=race.id)
        db.session.add(crew)
        db.session.commit()

        # Vygenerování QR kódu
        qr_buffer = generate_qr_with_center_text(str(crew.id), crew.number)
        file_path = f"qrcodes/{crew.name}_{crew.id}.png"
        with open(file_path, "wb") as f:
            f.write(qr_buffer.getvalue())

        public_url = upload_qr_to_supabase(file_path, f"{crew.name}_{crew.id}.png")
        crew.qr_code_url = public_url
        db.session.commit()

        recalculate_all_ideal_times(race.id)
        
        return redirect(f"/race/{race.id}/crews")
    
    return render_template("create_crew.html", race=race)

@app.route("/crew/<int:crew_id>/toggle_active", methods=["POST"])
def toggle_crew_active(crew_id):
    crew = Crew.query.get_or_404(crew_id)
    crew.is_active = not crew.is_active
    db.session.commit()
    recalculate_all_ideal_times(crew.race_id)
    return redirect(f"/race/{crew.race_id}/crews")

@app.route("/crew/<int:crew_id>/delete", methods=["POST"])
def delete_crew(crew_id):
    crew = Crew.query.get_or_404(crew_id)
    race_id = crew.race_id
    db.session.delete(crew)
    db.session.commit()
    recalculate_all_ideal_times(crew.race_id)
    return redirect(f"/race/{race_id}/crews")

@app.route("/crew/<int:crew_id>/edit", methods=["GET", "POST"])
def edit_crew(crew_id):
    crew = Crew.query.get_or_404(crew_id)
    error = None

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        number = request.form.get("number", "").strip()
        vehicle = request.form.get("vehicle", "").strip()
        is_active = "is_active" in request.form

        if not name or not number:
            error = "Jméno a číslo posádky jsou povinné!"
        else:
            old_name = crew.name
            crew.name = name
            crew.number = number
            crew.vehicle = vehicle
            crew.is_active = is_active
            db.session.commit()

            # Vygeneruj QR kód do paměti (in-memory)
            qr_buffer = generate_qr_with_center_text(str(crew.id), crew.number)
            filename = f"{crew.name}_{crew.id}.png"
            file_path = f"/tmp/{filename}"

            with open(file_path, "wb") as f:
                f.write(qr_buffer.getvalue())


            public_url = upload_qr_to_supabase(file_path, filename)
            print("QR kód nahrán:", public_url)

            crew.qr_code_url = public_url

            recalculate_all_ideal_times(crew.race_id)
            return redirect(f"/race/{crew.race_id}/crews")

    return render_template("edit_crew.html", crew=crew, error=error)

from zoneinfo import ZoneInfo

@app.route("/scan/<int:crew_id>/<int:checkpoint_id>", methods=["POST"])
def scan_qr(crew_id, checkpoint_id):
    crew = Crew.query.get_or_404(crew_id)
    checkpoint = Checkpoint.query.get_or_404(checkpoint_id)

    now = get_czech_time()  # << VYNUTÍME SI PRAŽSKÝ ČAS

    scan = ScanRecord(
        crew_id=crew.id,
        checkpoint_id=checkpoint.id,
        timestamp=now  # << TADY PŘEDÁŠ KONKRÉTNÍ ČAS, ne spoléháš na default
    )
    db.session.add(scan)
    db.session.commit()

    return jsonify({
        "status": "ok",
        "message": f"Zaznamenán průchod posádky {crew.name} na {checkpoint.name} v {scan.timestamp}"
    })


@app.route("/scan")
def scan_page():
    checkpoints = Checkpoint.query.order_by(Checkpoint.order).all()
    return render_template("scan_qr.html", checkpoints=checkpoints)


@app.route("/scan/<int:checkpoint_id>")
def scan_checkpoint(checkpoint_id):
    checkpoint = Checkpoint.query.get_or_404(checkpoint_id)
    checkpoints = Checkpoint.query.order_by(Checkpoint.order).all()

    total_crews = Crew.query.count()

    passed_crew_ids = db.session.query(ScanRecord.crew_id)\
        .filter_by(checkpoint_id=checkpoint_id)\
        .distinct().all()
    passed_count = len(passed_crew_ids)
    remaining = total_crews - passed_count

    return render_template(
        "scan_qr.html",
        checkpoint=checkpoint,
        checkpoints=checkpoints,
        total_crews=total_crews,
        passed_count=passed_count,
        remaining=remaining
    )


@app.route("/history")
def history():
    scans = ScanRecord.query.order_by(ScanRecord.timestamp.desc()).limit(100).all()
    checkpoints = Checkpoint.query.order_by(Checkpoint.order).all()
    return render_template("history.html", scans=scans, checkpoints=checkpoints)

@app.route("/history/checkpoint/<int:checkpoint_id>")
def history_checkpoint(checkpoint_id):
    checkpoint = Checkpoint.query.options(
        joinedload(Checkpoint.ideal_times).joinedload(IdealTime.crew)
    ).get_or_404(checkpoint_id)

    # Všechny posádky v závodě (aktivní i neaktivní), seřazené podle čísla
    all_crews = Crew.query.filter_by(race_id=checkpoint.race_id)\
        .order_by(cast(Crew.number, Integer)).all()
    total_crews = len(all_crews)

    # Posádky, které už prošly checkpointem
    passed_crews = db.session.query(Crew).join(ScanRecord)\
        .filter(ScanRecord.checkpoint_id == checkpoint_id)\
        .order_by(cast(Crew.number, Integer)).all()
    passed_count = len(passed_crews)

    # Načteme všechny ideální časy pro tento checkpoint najednou
    ideal_times = {it.crew_id: it for it in checkpoint.ideal_times}

    # Propojení dat
    crew_data = []
    for crew in all_crews:
        scan = next((s for s in passed_crews if s.id == crew.id), None)
        ideal_time = ideal_times.get(crew.id)

        crew_data.append({
            'crew': crew,
            'scan': scan,
            'ideal_time': ideal_time.ideal_time if ideal_time else None
        })

    # Najdeme ideální čas pro aktivní posádku č. 1
    first_crew_ideal = next((
    it for it in sorted(checkpoint.ideal_times, key=lambda x: int(x.crew.number))
    if it.crew.is_active
    ), None)

    # Najdeme ideální čas pro poslední aktivní posádku
    active_crews = [c for c in all_crews if c.is_active and c.number.isdigit()]
    if active_crews:
        sorted_active_crews = sorted(active_crews, key=lambda c: int(c.number))
        last_number = sorted_active_crews[-1].number
        last_crew_ideal = next(
            (it for it in checkpoint.ideal_times if it.crew.number == last_number),
            None
        )
    else:
        last_crew_ideal = None

    return render_template("history_checkpoint.html",
        checkpoint=checkpoint,
        crew_data=crew_data,
        total_crews=total_crews,
        passed_count=passed_count,
        remaining=total_crews - passed_count,
        first_crew_ideal=first_crew_ideal,
        last_crew_ideal=last_crew_ideal
    )


@app.route('/race/<int:race_id>/setup_ideal_times', methods=['GET', 'POST'])
def setup_ideal_times(race_id):
    race = Race.query.get_or_404(race_id)
    checkpoints = Checkpoint.query.filter_by(race_id=race_id).order_by(Checkpoint.order).all()
    crews = Crew.query.filter_by(race_id=race_id).order_by(Crew.number).all()

    if request.method == 'POST':
        try:
            # Získání startovního času
            start_time_str = request.form.get("start_time")
            if not start_time_str:
                return "Nebyl zadán startovní čas", 400
            
            try:
                start_time = datetime.strptime(start_time_str, "%H:%M").time()
            except ValueError:
                return "Neplatný formát času. Použijte HH:MM", 400

            # Získání intervalů mezi checkpointy
            intervals = []
            for i in range(1, len(checkpoints)):
                interval_key = f"interval_{checkpoints[i-1].id}_{checkpoints[i].id}"
                interval_str = request.form.get(interval_key)
                if not interval_str:
                    return f"Chybí interval mezi {checkpoints[i-1].name} a {checkpoints[i].name}", 400
                
                try:
                    minutes, seconds = map(int, interval_str.split(":"))
                    intervals.append(timedelta(minutes=minutes, seconds=seconds))
                except (ValueError, AttributeError):
                    return f"Neplatný formát intervalu mezi {checkpoints[i-1].name} a {checkpoints[i].name}. Použijte MM:SS", 400

            # NEBEGINUJEME novou transakci, Flask-SQLAlchemy už ji spravuje
            
            # Smazání starých časů pro tento závod
            IdealTime.query.filter(
                IdealTime.crew_id.in_([c.id for c in crews]),
                IdealTime.checkpoint_id.in_([ck.id for ck in checkpoints])
            ).delete(synchronize_session=False)

            # Výpočet základního data
            base_date = race.start_time.date() if race.start_time else datetime.now().date()
            current_time = datetime.combine(base_date, start_time)

            # Výpočet časů pro posádku č. 1
            checkpoint_times = [current_time]
            for i in range(1, len(checkpoints)):
                checkpoint_times.append(checkpoint_times[i-1] + intervals[i-1])

            # Seřadíme posádky podle čísla
            sorted_crews = sorted(crews, key=lambda c: int(c.number))
            
            # Uložení časů pro všechny posádky
            for index, crew in enumerate(sorted_crews):  # index 0 = první posádka
                for checkpoint, ideal_time in zip(checkpoints, checkpoint_times):
                    # Rozestup 1 minuta mezi každou posádkou
                    crew_ideal_time = ideal_time + timedelta(minutes=index)
                    
                    ideal_record = IdealTime(
                        crew_id=crew.id,
                        checkpoint_id=checkpoint.id,
                        ideal_time=crew_ideal_time.time()
                    )
                    db.session.add(ideal_record)
                    
            # Aktualizace názvů checkpointů podle formuláře

            for ck in checkpoints:
                new_name = request.form.get(f"name_{ck.id}")
                if new_name:
                    ck.name = new_name

            db.session.commit()
            return redirect(url_for('checkpoint_overview', race_id=race_id, success=True))

        except Exception as e:
            db.session.rollback()
            return f"Došlo k chybě: {str(e)}", 500

    return render_template("setup_ideal_times.html", 
                         race=race,
                         checkpoints=checkpoints,
                         crews=crews)

@app.route("/race/<int:race_id>/checkpoints")
def checkpoint_overview(race_id):
    race = Race.query.get_or_404(race_id)
    checkpoints = Checkpoint.query.filter_by(race_id=race_id).order_by(Checkpoint.order).all()

    # Načtení ideálních časů posádky č. 1
    first_crew = Crew.query.filter_by(race_id=race_id, number="1").first()
    times = {}
    if first_crew:
        times = {it.checkpoint_id: it.ideal_time for it in IdealTime.query.filter_by(crew_id=first_crew.id).all()}

    return render_template("checkpoints.html", race=race, checkpoints=checkpoints, ideal_times=times)

@app.route('/race/<int:race_id>/export_results')
def export_results(race_id):
    race = Race.query.get_or_404(race_id)
    crews = Crew.query.filter_by(race_id=race_id).all()
    checkpoints = Checkpoint.query.filter_by(race_id=race_id).order_by(Checkpoint.order).all()

    ideal_times = IdealTime.query.filter(
        IdealTime.crew_id.in_([c.id for c in crews]),
        IdealTime.checkpoint_id.in_([ck.id for ck in checkpoints])
    ).all()

    scan_records = ScanRecord.query.filter(
        ScanRecord.crew_id.in_([c.id for c in crews]),
        ScanRecord.checkpoint_id.in_([ck.id for ck in checkpoints])
    ).order_by(ScanRecord.timestamp).all()

    ideal_dict = {(i.crew_id, i.checkpoint_id): i.ideal_time for i in ideal_times}
    scan_dict = {}
    for s in scan_records:
        key = (s.crew_id, s.checkpoint_id)
        if key not in scan_dict:
            scan_dict[key] = s.timestamp.time()

    rows = []

    for crew in crews:
        vehicle_year = safe_int(crew.vehicle_year)
        penalty_year = safe_int(crew.penalty_year)
    
        row = {
            "Číslo": crew.number,
            "Jméno": crew.name,
            "Vozidlo": crew.vehicle,
            "Třída": crew.category,
            "Rok výroby": vehicle_year,
            "Trestne body za rv auta": penalty_year
        }
    
        total_penalty = 0
    
        for ck in checkpoints:
            ideal = ideal_dict.get((crew.id, ck.id))
            real = scan_dict.get((crew.id, ck.id))
    
            row[f"{ck.name} - Ideál"] = ideal.strftime("%H:%M") if ideal else "-"
            row[f"{ck.name} - Skutečnost"] = real.strftime("%H:%M") if real else "-"
    
            if ideal and real:
                diff = abs(
                    (datetime.combine(datetime.today(), real) -
                     datetime.combine(datetime.today(), ideal)).total_seconds()
                ) / 60
                penalty = min(int(diff) * 10, 100)
            else:
                penalty = 100
    
            row[f"{ck.name} - Body"] = penalty
            total_penalty += penalty
    
        total_penalty += penalty_year
        row["Celkem body"] = total_penalty
        rows.append(row)



    df = pd.DataFrame(rows)

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Výsledky')
        worksheet = writer.sheets['Výsledky']

        # Automatická šířka sloupců
        for i, col in enumerate(df.columns, 1):
            max_length = max(
                df[col].astype(str).map(len).max(),
                len(str(col))
            )
            worksheet.column_dimensions[get_column_letter(i)].width = max_length + 2

    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name=f"vysledky_zavodu_{race.id}.xlsx",
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@app.route("/test-time")
def test_time():
    now = get_czech_time()
    return f"Aktuální čas v Praze: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"

    
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
