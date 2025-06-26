from flask import Flask, request, redirect, render_template, jsonify, url_for, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Time, cast, Integer
from sqlalchemy.orm import joinedload
from datetime import datetime, timedelta
import qrcode
import io
import os
import pytz
from dotenv import load_dotenv
from supabase_upload import upload_qr_to_supabase
import sqlalchemy as sa

# Flask a SQLAlchemy setup
app = Flask(__name__)
db_url = os.getenv("SQLALCHEMY_DATABASE_URI")
if not db_url:
    raise RuntimeError("Chybí proměnná SQLALCHEMY_DATABASE_URI")
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
db = SQLAlchemy(app)

#app = Flask(__name__)
#app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
#db = SQLAlchemy(app)

# složka, kam budeme ukládat QR kódy
#QR_FOLDER = os.path.join(app.static_folder, "qrcodes")
#os.makedirs(QR_FOLDER, exist_ok=True)

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

class Checkpoint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    order = db.Column(db.Integer)
    race_id = db.Column(db.Integer, db.ForeignKey('race.id'), nullable=False)

class ScanRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    crew_id = db.Column(db.Integer, db.ForeignKey('crew.id'), nullable=False)
    checkpoint_id = db.Column(db.Integer, db.ForeignKey('checkpoint.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

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

def recalculate_all_ideal_times(race_id):
    race = Race.query.get(race_id)
    if not race:
        return

    checkpoints = Checkpoint.query.filter_by(race_id=race.id).order_by(Checkpoint.order).all()

    # Získáme posádku číslo 1 jako referenci
    reference_crew = Crew.query.filter_by(race_id=race.id, number="1").first()
    if not reference_crew:
        return

    base_times = IdealTime.query.filter(
        IdealTime.crew_id == reference_crew.id,
        IdealTime.checkpoint_id.in_([ck.id for ck in checkpoints])
    ).order_by(IdealTime.checkpoint_id).all()

    if not base_times:
        return

    # Smažeme všechny dosavadní ideální časy pro tento závod
    crews = Crew.query.filter_by(race_id=race.id).all()
    crew_ids = [c.id for c in crews]
    IdealTime.query.filter(IdealTime.crew_id.in_(crew_ids)).delete()
    db.session.commit()

    # Seřadíme aktivní posádky podle čísla
    sorted_crews = sorted(
        [c for c in crews if c.is_active],
        key=lambda x: int(x.number) if x.number.isdigit() else 9999
    )

    base_date = datetime.today().date()

    for idx, crew in enumerate(sorted_crews):
        offset = idx  # první aktivní = offset 0
        for bt in base_times:
            orig_dt = datetime.combine(base_date, bt.ideal_time)
            new_dt = orig_dt + timedelta(minutes=offset * race.crew_interval)

            new_ideal = IdealTime(
                crew_id=crew.id,
                checkpoint_id=bt.checkpoint_id,
                ideal_time=new_dt.time()
            )
            db.session.add(new_ideal)

    db.session.commit()

@app.route("/")
def index():
    races = Race.query.order_by(Race.start_time.desc()).all()
    return render_template("index.html", races=races)

#smazaní databáze
@app.route('/delete-database', methods=['POST'])
def delete_database():
    # Bezpečné smazání všech tabulek
    try:
        db.session.close()
        db.drop_all()
        db.create_all()  # Vytvoří prázdnou DB se správnou strukturou
        db.session.commit()
        flash('Databáze byla úspěšně smazána a znovu vytvořena', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Chyba při mazání databáze: {str(e)}', 'danger')
    
    return redirect(url_for('index'))  # Presměrujte kam potřebujete
    
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
    crews = Crew.query.filter_by(race_id=race.id).order_by(Crew.number).all()
    checkpoints = Checkpoint.query.filter_by(race_id=race.id).order_by(Checkpoint.order).all()
    
    # Získání ideálních časů první posádky
    first_crew = next((crew for crew in crews if crew.number == '1'), None)
    checkpoint_times = {}
    if first_crew:
        ideal_times = IdealTime.query.filter_by(crew_id=first_crew.id).all()
        checkpoint_times = {time.checkpoint_id: time.ideal_time.strftime('%H:%M') for time in ideal_times}
    
    return render_template(
        "race_detail.html", 
        race=race, 
        crews=crews, 
        checkpoints=checkpoints,
        checkpoint_times=checkpoint_times
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
        qr_text = str(crew.id)
        qr_img = qrcode.make(qr_text)
        file_path = f"qrcodes/{crew.name}_{crew.id}.png"
        qr_img.save(file_path)
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
    recalculate_all_ideal_times(race.id)
    return redirect(f"/race/{crew.race_id}/crews")

@app.route("/crew/<int:crew_id>/delete", methods=["POST"])
def delete_crew(crew_id):
    crew = Crew.query.get_or_404(crew_id)
    race_id = crew.race_id
    db.session.delete(crew)
    db.session.commit()
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
            qr_text = str(crew.id)
            qr_img = qrcode.make(qr_text)
            buffer = io.BytesIO()
            qr_img.save(buffer, format="PNG")
            buffer.seek(0)

            # Nahraj do Supabase
            filename = f"{crew.name}_{crew.id}.png"
            file_path = f"/tmp/{filename}"

            # Ulož dočasně na disk kvůli uploadu
            with open(file_path, "wb") as f:
                f.write(buffer.getvalue())

            public_url = upload_qr_to_supabase(file_path, filename)
            print("QR kód nahrán:", public_url)

            crew.qr_code_url = public_url

            recalculate_all_ideal_times(crew.race.id)
            return redirect(f"/race/{crew.race_id}/crews")

    return render_template("edit_crew.html", crew=crew, error=error)

@app.route("/scan/<int:crew_id>/<int:checkpoint_id>", methods=["POST"])
def scan_qr(crew_id, checkpoint_id):
    crew = Crew.query.get_or_404(crew_id)
    checkpoint = Checkpoint.query.get_or_404(checkpoint_id)

    scan = ScanRecord(crew_id=crew.id, checkpoint_id=checkpoint.id)
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

    # Všechny posádky v závodě seřazené podle čísla
    all_crews = Crew.query.filter_by(race_id=checkpoint.race_id).order_by(cast(Crew.number, Integer)).all()
    total_crews = len(all_crews)

    # Posádky, které už prošly checkpointem
    passed_crews = db.session.query(Crew).join(ScanRecord)\
        .filter(ScanRecord.checkpoint_id == checkpoint_id)\
        .order_by(Crew.number).all()
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

    # Najdeme ideální čas pro posádku č. 1
    first_crew_ideal = next((it for it in checkpoint.ideal_times 
                           if it.crew.number == '1'), None)
    # Najdeme ideální čas pro poslední posádku
    if all_crews:
        # Seřadíme posádky podle čísla (jako integer pro správné řazení)
        sorted_crews = sorted(all_crews, key=lambda c: int(c.number))
        last_crew_number = sorted_crews[-1].number
        last_crew_ideal = next(
            (it for it in checkpoint.ideal_times if it.crew.number == last_crew_number),
            None
        )
    else:
        last_crew_ideal = None
        # Získání všech scanů pro tento checkpoint
    scans = ScanRecord.query.filter_by(checkpoint_id=checkpoint_id)\
        .order_by(ScanRecord.timestamp.desc()).all()

    return render_template("history_checkpoint.html",
                         checkpoint=checkpoint,
                         crew_data=crew_data,
                         total_crews=total_crews,
                         passed_count=passed_count,
                         remaining=total_crews - passed_count,
                         first_crew_ideal=first_crew_ideal,
                         last_crew_ideal=last_crew_ideal)


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
    
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
