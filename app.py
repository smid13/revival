from flask import Flask, request, redirect, render_template, jsonify, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta
import qrcode
import os

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
db = SQLAlchemy(app)

# složka, kam budeme ukládat QR kódy
QR_FOLDER = os.path.join("qrcodes")
os.makedirs(QR_FOLDER, exist_ok=True)

def generate_ideal_times(first_ck_time_str, intervals, crews, checkpoints):
    # převod textového času na datetime
    current_time = datetime.strptime(first_ck_time_str, "%Y-%m-%d %H:%M")

    # první posádka má číslo 1
    sorted_crews = sorted(crews, key=lambda c: int(c.number))

    for i, crew in enumerate(sorted_crews):
        crew_time = current_time + timedelta(minutes=i)
        for j, ck in enumerate(checkpoints):
            ideal = IdealTime(crew_id=crew.id, checkpoint_id=ck.id, ideal_time=crew_time)
            db.session.add(ideal)
            if j < len(intervals):
                crew_time += timedelta(minutes=intervals[j])
    db.session.commit()

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
    ideal_time = db.Column(db.DateTime, nullable=False)

    crew = db.relationship('Crew', backref='ideal_times')
    checkpoint = db.relationship('Checkpoint', backref='ideal_times')

class IdealArrival(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    checkpoint_id = db.Column(db.Integer, db.ForeignKey('checkpoint.id'), nullable=False)
    crew_id = db.Column(db.Integer, db.ForeignKey('crew.id'), nullable=False)
    ideal_time = db.Column(db.Time, nullable=False)

    checkpoint = db.relationship('Checkpoint', backref='ideal_arrivals')
    crew = db.relationship('Crew', backref='ideal_arrivals')

@app.route("/")
def index():
    races = Race.query.order_by(Race.start_time.desc()).all()
    return render_template("index.html", races=races)


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
    return render_template("race_detail.html", race=race, crews=crews, checkpoints=checkpoints)

@app.route("/race/<int:race_id>/crews", methods=["GET"])
def manage_crews(race_id):
    race = Race.query.get_or_404(race_id)
    crews = Crew.query.filter_by(race_id=race.id).order_by(Crew.number).all()
    return render_template("manage_crews.html", race=race, crews=crews)

@app.route("/race/<int:race_id>/crews/create", methods=["GET", "POST"])
def create_crew(race_id):
    race = Race.query.get_or_404(race_id)
    if request.method == "POST":
        name = request.form["name"]
        vehicle = request.form["vehicle"]
        number = request.form["number"]

        crew = Crew(name=name, vehicle=vehicle, number=number, race_id=race.id)
        db.session.add(crew)
        db.session.commit()

        # === Vygenerování QR kódu ===
        qr_text = str(crew.id)
        qr_img = qrcode.make(qr_text)
        qr_path = os.path.join(QR_FOLDER, f"crew_{crew.id}.png")
        qr_img.save(qr_path)


        return redirect(f"/race/{race.id}/crews")
    return render_template("create_crew.html", race=race)

@app.route("/crew/<int:crew_id>/toggle_active", methods=["POST"])
def toggle_crew_active(crew_id):
    crew = Crew.query.get_or_404(crew_id)
    crew.is_active = not crew.is_active
    db.session.commit()
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
            crew.name = name
            crew.number = number
            crew.vehicle = vehicle
            crew.is_active = is_active

            db.session.commit()
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
    checkpoint = Checkpoint.query.get_or_404(checkpoint_id)

    # Celkový počet posádek v závodě
    total_crews = Crew.query.filter_by(race_id=checkpoint.race_id).count()

    # Počet posádek, které už mají průjezd přes checkpoint
    passed_crew_ids = db.session.query(ScanRecord.crew_id).filter_by(checkpoint_id=checkpoint_id).distinct().all()
    passed_count = len(passed_crew_ids)

    remaining = total_crews - passed_count

    scans = ScanRecord.query.filter_by(checkpoint_id=checkpoint_id).order_by(ScanRecord.timestamp.desc()).all()
    ideal_time = db.session.query(IdealTime.ideal_time)\
    .join(Crew, IdealTime.crew_id == Crew.id)\
    .filter(IdealTime.checkpoint_id == checkpoint.id, Crew.number == '1')\
    .first()

    return render_template("history_checkpoint.html",
                           checkpoint=checkpoint,
                           scans=scans,
                           total_crews=total_crews,
                           passed_count=passed_count,
                           remaining=remaining,
                           ideal_time=ideal_time[0] if ideal_time else None)

@app.route('/setup-ideal-times', methods=['GET', 'POST'])
def setup_ideal_times():
    checkpoints = Checkpoint.query.order_by(Checkpoint.id).all()
    crews = Crew.query.order_by(Crew.number).all()

    if request.method == 'POST':
        # Získáme čas první ČK
        start_time_str = request.form.get("start_time")
        start_time = datetime.strptime(start_time_str, "%H:%M").time()

        # Získáme intervaly
        intervals = []
        for i in range(1, len(checkpoints)):
            val = request.form.get(f"interval_{i}")
            if not val:
                return f"Chybí interval pro ČK {i+1}", 400
            m, s = map(int, val.split(":"))
            intervals.append(timedelta(minutes=m, seconds=s))

        # Vytvoříme časy pro posádku č. 1
        ideal_times_by_checkpoint = [start_time]
        current_dt = datetime.combine(datetime.today(), start_time)
        for interval in intervals:
            current_dt += interval
            ideal_times_by_checkpoint.append(current_dt.time())

        # Pro každou posádku spočítáme čas s minutovým rozestupem
        for offset, crew in enumerate(crews):
            for idx, checkpoint in enumerate(checkpoints):
                ideal_dt = (datetime.combine(datetime.today(), ideal_times_by_checkpoint[idx]) +
                            timedelta(minutes=offset))
                existing = IdealArrival.query.filter_by(checkpoint_id=checkpoint.id, crew_id=crew.id).first()
                if existing:
                    existing.ideal_time = ideal_dt.time()
                else:
                    db.session.add(IdealArrival(
                        checkpoint_id=checkpoint.id,
                        crew_id=crew.id,
                        ideal_time=ideal_dt.time()
                    ))
        db.session.commit()
        return redirect(url_for("index"))

    return render_template("setup_ideal_times.html", checkpoints=checkpoints)

@app.route("/race/<int:race_id>/checkpoints")
def checkpoint_overview(race_id):
    race = Race.query.get_or_404(race_id)
    checkpoints = Checkpoint.query.filter_by(race_id=race_id).order_by(Checkpoint.order).all()

    # Načtení ideálních časů posádky č. 1
    first_crew = Crew.query.filter_by(race_id=race_id, number="1").first()
    times = {}
    if first_crew:
        times = {it.checkpoint_id: it.ideal_arrival for it in IdealTime.query.filter_by(crew_id=first_crew.id).all()}

    return render_template("checkpoints.html", race=race, checkpoints=checkpoints, ideal_times=times)
    
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True) 