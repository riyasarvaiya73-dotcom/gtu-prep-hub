import os
from dotenv import load_dotenv
load_dotenv()

import csv
import io
import json
import re
from datetime import date, datetime

import requests
from flask import (
    Flask, render_template, redirect, url_for, 
    request, flash, jsonify, Response, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from flask_login import (
    LoginManager, UserMixin, login_user, 
    login_required, logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from pypdf import PdfReader

app = Flask(__name__)

UPLOAD_FOLDER = os.path.join('static', 'uploads')
ALLOWED_EXTENSIONS = {'pdf'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 150 * 1024 * 1024  # 16 MB max upload
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fallback-dev-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(basedir, 'gtu_prep.db')}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Helper function to strip markdown code fences from Gemini responses
def clean_gemini_json(raw_text):
    clean = re.sub(r'^```(?:json)?\s*', '', raw_text.strip(), flags=re.MULTILINE)
    clean = re.sub(r'```$', '', clean.strip(), flags=re.MULTILINE)
    return clean.strip()

# ----------------- DATABASE MODELS -----------------

class ExamPaper(db.Model):
    __tablename__ = 'exam_papers'
    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(db.Integer, db.ForeignKey('subject.id'), nullable=False)
    session_name = db.Column(db.String(100), nullable=False)
    pdf_filename = db.Column(db.String(255), nullable=False)
    paper_json = db.Column(db.Text, nullable=True)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    subject = db.relationship('Subject', backref=db.backref('exam_papers', lazy=True))

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    enrollment_no = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    department = db.Column(db.String(50), default="Computer Engineering")
    semester = db.Column(db.Integer, default=5)
    is_admin = db.Column(db.Boolean, default=False)
    last_login = db.Column(db.DateTime, default=datetime.utcnow)
    progress_records = db.relationship('UserTopicProgress', backref='user', lazy=True)

class ActivityLog(db.Model):
    __tablename__ = 'activity_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action_type = db.Column(db.String(50), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subject.id'), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref=db.backref('activities', lazy=True))
    subject = db.relationship('Subject', backref=db.backref('activities', lazy=True))

class Subject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(15), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    department = db.Column(db.String(50), nullable=False)
    semester = db.Column(db.Integer, nullable=False)
    exam_date = db.Column(db.Date, nullable=True)
    topics = db.relationship('Topic', backref='subject', lazy=True)
    resources = db.relationship('Resource', backref='subject', lazy=True)

class Topic(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(db.Integer, db.ForeignKey('subject.id'), nullable=False)
    unit_no = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(200), nullable=False)
    weightage = db.Column(db.Integer, default=10)

class UserTopicProgress(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    topic_id = db.Column(db.Integer, db.ForeignKey('topic.id'), nullable=False)
    is_completed = db.Column(db.Boolean, default=False)

class Resource(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(db.Integer, db.ForeignKey('subject.id'), nullable=False)
    category = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    link = db.Column(db.String(300), nullable=False)

class StudentNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subject.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship('User', backref='notes')
    subject = db.relationship('Subject', backref='student_notes')

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ----------------- STUDENT ROUTES -----------------

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        enrollment = request.form.get('enrollment_no')
        name = request.form.get('name')
        dept = request.form.get('department')
        sem = int(request.form.get('semester'))
        password = request.form.get('password')

        if User.query.filter_by(enrollment_no=enrollment).first():
            flash('Enrollment number already registered.', 'danger')
            return redirect(url_for('register'))

        new_user = User(
            enrollment_no=enrollment,
            name=name,
            department=dept,
            semester=sem,
            password_hash=generate_password_hash(password)
        )
        db.session.add(new_user)
        db.session.commit()
        flash('Account created successfully! Please login.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        enrollment = request.form.get('enrollment_no')
        password = request.form.get('password')
        user = User.query.filter_by(enrollment_no=enrollment).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            user.last_login = datetime.utcnow()
            log = ActivityLog(user_id=user.id, action_type='login')
            db.session.add(log)
            db.session.commit()
            if user.is_admin:
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('dashboard'))
        flash('Invalid enrollment number or password.', 'danger')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    selected_dept = request.args.get('department', current_user.department)
    selected_sem = request.args.get('semester', current_user.semester, type=int)

    subjects = Subject.query.filter_by(
        department=selected_dept,
        semester=selected_sem
    ).all()

    return render_template(
        'dashboard.html',
        subjects=subjects,
        selected_dept=selected_dept,
        selected_sem=selected_sem
    )

@app.route('/subject/<int:subject_id>')
@login_required
def subject_detail(subject_id):
    subject = Subject.query.get_or_404(subject_id)
    topics = Topic.query.filter_by(subject_id=subject.id).order_by(Topic.unit_no).all()
    
    completed_ids = set([
        p.topic_id for p in UserTopicProgress.query.filter_by(
            user_id=current_user.id, is_completed=True
        ).all()
    ])

    total_weight = sum(t.weightage for t in topics) or 1
    completed_weight = sum(t.weightage for t in topics if t.id in completed_ids)
    readiness_percentage = round((completed_weight / total_weight) * 100)

    if readiness_percentage < 40:
        prediction_status = "High Risk (Backlog Alert)"
        strategy = "Focus exclusively on 7-mark recurring GTU questions and high-weightage units."
    elif readiness_percentage < 75:
        prediction_status = "Average / Clearing Zone"
        strategy = "Practice mid-level units and 3-4 mark PYQs to reach 8+ SPI."
    else:
        prediction_status = "Distinction Zone (9+ SPI)"
        strategy = "Attempt complete 70-mark timed GTU papers from previous years."

    days_left = None
    daily_target_units = 0
    urgent_topics = []

    if subject.exam_date:
        today = date.today()
        delta = (subject.exam_date - today).days
        days_left = max(0, delta)
        pending_topics = [t for t in topics if t.id not in completed_ids]
        urgent_topics = sorted(pending_topics, key=lambda t: t.weightage, reverse=True)

        if days_left > 0 and pending_topics:
            daily_target_units = round(len(pending_topics) / days_left, 1)

    resources = Resource.query.filter_by(subject_id=subject.id).all()
    user_note = StudentNote.query.filter_by(
        user_id=current_user.id,
        subject_id=subject.id
    ).first()

    return render_template(
        'subject_detail.html',
        subject=subject,
        topics=topics,
        completed_ids=completed_ids,
        readiness=readiness_percentage,
        prediction_status=prediction_status,
        strategy=strategy,
        resources=resources,
        days_left=days_left,
        daily_target_units=daily_target_units,
        urgent_topics=urgent_topics,
        user_note=user_note
    )

@app.route('/api/toggle-topic', methods=['POST'])
@login_required
def toggle_topic():
    data = request.get_json() or {}
    topic_id = data.get('topic_id')
    
    progress = UserTopicProgress.query.filter_by(
        user_id=current_user.id,
        topic_id=topic_id
    ).first()
 
    if not progress:
        progress = UserTopicProgress(user_id=current_user.id, topic_id=topic_id, is_completed=True)
        db.session.add(progress)
    else:
        progress.is_completed = not progress.is_completed
    
    db.session.commit()
    return jsonify({'success': True, 'is_completed': progress.is_completed})

@app.route('/subject/<int:subject_id>/save-note', methods=['POST'])
@login_required
def save_student_note(subject_id):
    subject = Subject.query.get_or_404(subject_id)
    content = request.form.get('note_content', '').strip()

    existing_note = StudentNote.query.filter_by(
        user_id=current_user.id,
        subject_id=subject.id
    ).first()

    if existing_note:
        existing_note.content = content
        existing_note.updated_at = datetime.utcnow()
    else:
        new_note = StudentNote(
            user_id=current_user.id,
            subject_id=subject.id,
            content=content
        )
        db.session.add(new_note)

    db.session.commit()
    flash("Personal revision note saved!", "success")
    return redirect(url_for('subject_detail', subject_id=subject.id))

@app.route('/download-paper/<int:paper_id>')
@login_required
def download_paper(paper_id):
    paper = ExamPaper.query.get_or_404(paper_id)
    return send_from_directory(app.config['UPLOAD_FOLDER'], paper.pdf_filename, as_attachment=True)

@app.route('/api/mock-paper/<int:paper_id>')
@login_required
def get_mock_paper(paper_id):
    paper = ExamPaper.query.get_or_404(paper_id)
    if not paper.paper_json:
        return jsonify({'error': 'Interactive test mode not available for this paper.'}), 404

    log = ActivityLog(user_id=current_user.id, action_type='attempted_mock_exam', subject_id=paper.subject_id)
    db.session.add(log)
    db.session.commit()

    return jsonify({
        'session_name': paper.session_name,
        'subject_code': paper.subject.code,
        'subject_name': paper.subject.name,
        'questions': json.loads(paper.paper_json)
    })

# ----------------- ADMIN CONTROLLERS -----------------

@app.route('/admin')
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        flash('Access denied. Administrator privileges required.', 'danger')
        return redirect(url_for('dashboard'))

    subjects = Subject.query.order_by(Subject.semester, Subject.code).all()
    return render_template('admin/admin_dashboard.html', subjects=subjects)

@app.route('/admin/add-subject', methods=['GET', 'POST'])
@login_required
def admin_add_subject():
    if not current_user.is_admin:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        name = request.form.get('name', '').strip()
        dept = request.form.get('department')
        sem = int(request.form.get('semester'))

        if Subject.query.filter_by(code=code).first():
            flash(f'Subject code {code} already exists!', 'warning')
            return redirect(url_for('admin_add_subject'))

        new_subject = Subject(code=code, name=name, department=dept, semester=sem)
        db.session.add(new_subject)
        db.session.commit()

        flash(f'Subject "{name}" added successfully! Now add units and materials.', 'success')
        return redirect(url_for('admin_manage_subject', subject_id=new_subject.id))

    return render_template('admin/add_subject.html')

@app.route('/admin/manage-subject/<int:subject_id>', methods=['GET', 'POST'])
@login_required
def admin_manage_subject(subject_id):
    if not current_user.is_admin:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    subject = Subject.query.get_or_404(subject_id)

    if request.method == 'POST':
        action_type = request.form.get('action_type')

        if action_type == 'add_unit':
            unit_no = int(request.form.get('unit_no'))
            title = request.form.get('title', '').strip()
            weightage = int(request.form.get('weightage', 10))

            topic = Topic(subject_id=subject.id, unit_no=unit_no, title=title, weightage=weightage)
            db.session.add(topic)
            db.session.commit()
            flash(f'Unit {unit_no} added!', 'success')

        elif action_type == 'add_resource':
            category = request.form.get('category')
            res_title = request.form.get('title', '').strip()
            file = request.files.get('file')

            if not file or file.filename == '':
                flash('Please select a PDF file to upload.', 'warning')
                return redirect(url_for('admin_manage_subject', subject_id=subject.id))

            if file and allowed_file(file.filename):
                safe_name = secure_filename(file.filename)
                final_filename = f"{subject.code}_{safe_name}"
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], final_filename)
                file.save(file_path)

                relative_url = url_for('static', filename=f'uploads/{final_filename}')
                res = Resource(subject_id=subject.id, category=category, title=res_title, link=relative_url)
                db.session.add(res)
                db.session.commit()
                flash(f'PDF "{res_title}" uploaded successfully!', 'success')
            else:
                flash('Invalid file type. Only PDF files are allowed.', 'danger')

            return redirect(url_for('admin_manage_subject', subject_id=subject.id))

    topics = Topic.query.filter_by(subject_id=subject.id).order_by(Topic.unit_no).all()
    resources = Resource.query.filter_by(subject_id=subject.id).all()

    return render_template(
        'admin/manage_subject.html',
        subject=subject,
        topics=topics,
        resources=resources
    )

@app.route('/admin/set-exam-date/<int:subject_id>', methods=['POST'])
@login_required
def set_exam_date(subject_id):
    if not current_user.is_admin:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    subject = Subject.query.get_or_404(subject_id)
    raw_date = request.form.get('exam_date')

    if raw_date:
        subject.exam_date = datetime.strptime(raw_date, '%Y-%m-%d').date()
        db.session.commit()
        flash(f"Exam date updated for {subject.name}!", "success")

    return redirect(url_for('admin_manage_subject', subject_id=subject.id))

@app.route('/admin/topic/<int:topic_id>/edit', methods=['POST'])
@login_required
def edit_topic(topic_id):
    if not current_user.is_admin:
        flash("Access restricted to administrators.", "danger")
        return redirect(url_for('dashboard'))

    topic = Topic.query.get_or_404(topic_id)
    topic.unit_no = request.form.get('unit_no', topic.unit_no, type=int)
    topic.title = request.form.get('title', topic.title).strip()
    topic.weightage = request.form.get('weightage', topic.weightage, type=int)

    db.session.commit()
    flash(f"Unit {topic.unit_no} updated successfully!", "success")
    return redirect(url_for('admin_manage_subject', subject_id=topic.subject_id))

@app.route('/admin/topic/<int:topic_id>/delete', methods=['POST'])
@login_required
def delete_topic(topic_id):
    if not current_user.is_admin:
        flash("Access restricted to administrators.", "danger")
        return redirect(url_for('dashboard'))

    topic = Topic.query.get_or_404(topic_id)
    subject_id = topic.subject_id

    db.session.delete(topic)
    db.session.commit()
    flash("Unit deleted from syllabus.", "info")
    return redirect(url_for('admin_manage_subject', subject_id=subject_id))

@app.route('/admin/subject/<int:subject_id>/add-topic', methods=['POST'])
@login_required
def add_manual_topic(subject_id):
    if not current_user.is_admin:
        flash("Access restricted to administrators.", "danger")
        return redirect(url_for('dashboard'))

    subject = Subject.query.get_or_404(subject_id)
    unit_no = request.form.get('unit_no', type=int)
    title = request.form.get('title', '').strip()
    weightage = request.form.get('weightage', 10, type=int)

    if not title:
        flash("Unit title cannot be blank.", "warning")
        return redirect(url_for('admin_manage_subject', subject_id=subject.id))

    new_topic = Topic(
        subject_id=subject.id,
        unit_no=unit_no,
        title=title,
        weightage=weightage
    )
    db.session.add(new_topic)
    db.session.commit()
    flash(f"Added Unit {unit_no} to syllabus.", "success")
    return redirect(url_for('admin_manage_subject', subject_id=subject.id))

@app.route('/admin/subject/<int:subject_id>/delete', methods=['POST'])
@login_required
def admin_delete_subject(subject_id):
    if not current_user.is_admin:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    subject = Subject.query.get_or_404(subject_id)
    subject_name = subject.name

    try:
        Topic.query.filter_by(subject_id=subject.id).delete()

        papers = ExamPaper.query.filter_by(subject_id=subject.id).all()
        for p in papers:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], p.pdf_filename)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass
            db.session.delete(p)

        StudentNote.query.filter_by(subject_id=subject.id).delete()
        ActivityLog.query.filter_by(subject_id=subject.id).delete()

        db.session.delete(subject)
        db.session.commit()

        flash(f"Subject '{subject_name}' and all associated materials were permanently deleted.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error deleting subject: {str(e)}", "danger")

    return redirect(url_for('admin_dashboard'))

# ----------------- AI PARSERS -----------------

@app.route('/admin/subject/<int:subject_id>/parse-syllabus', methods=['POST'])
@login_required
def parse_syllabus_pdf(subject_id):
    if not current_user.is_admin:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    subject = Subject.query.get_or_404(subject_id)
    pdf_file = request.files.get('syllabus_pdf')

    if not pdf_file or pdf_file.filename == '':
        flash("Please choose a valid PDF file.", "warning")
        return redirect(url_for('admin_manage_subject', subject_id=subject.id))

    try:
        reader = PdfReader(pdf_file)
        extracted_text = ""
        for page in reader.pages:
            text = page.extract_text()
            if text:
                extracted_text += text + "\n"
    except Exception as e:
        flash(f"Error reading PDF: {str(e)}", "danger")
        return redirect(url_for('admin_manage_subject', subject_id=subject.id))

    if not extracted_text.strip():
        flash("Could not extract any readable text from the uploaded PDF.", "warning")
        return redirect(url_for('admin_manage_subject', subject_id=subject.id))

    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        flash("GEMINI_API_KEY is not set.", "danger")
        return redirect(url_for('admin_manage_subject', subject_id=subject.id))

    system_instruction = (
        "You are an academic data extraction engine for GTU syllabus documents. "
        "Extract all syllabus units/modules from the provided text. "
        "Return ONLY a valid JSON array of objects with these exact keys:\n"
        "- 'unit_no': integer\n"
        "- 'title': concise string title of the unit\n"
        "- 'weightage': integer\n"
        "Do NOT include markdown formatting like ```json. Output raw JSON only."
    )

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    payload = {
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"parts": [{"text": extracted_text[:12000]}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        resp_data = response.json()

        if response.status_code != 200:
            error_msg = resp_data.get('error', {}).get('message', 'Failed to reach Gemini API.')
            flash(f"Gemini API Error ({response.status_code}): {error_msg}", "danger")
            return redirect(url_for('admin_manage_subject', subject_id=subject.id))

        raw_json_str = resp_data['candidates'][0]['content']['parts'][0]['text']
        cleaned_json = clean_gemini_json(raw_json_str)
        parsed_topics = json.loads(cleaned_json)

        for item in parsed_topics:
            topic = Topic(
                unit_no=int(item.get('unit_no', 1)),
                title=str(item.get('title', 'Unit Topic')),
                weightage=int(item.get('weightage', 15)),
                subject_id=subject.id
            )
            db.session.add(topic)

        db.session.commit()
        flash(f"Successfully extracted {len(parsed_topics)} units from syllabus PDF!", "success")

    except Exception as e:
        flash(f"Failed to parse syllabus with AI: {str(e)}", "danger")

    return redirect(url_for('admin_manage_subject', subject_id=subject.id))

@app.route('/admin/subject/<int:subject_id>/upload-pyq-paper', methods=['POST'])
@login_required
def admin_upload_pyq_paper(subject_id):
    if not current_user.is_admin:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    subject = Subject.query.get_or_404(subject_id)
    session_name = request.form.get('session_name', '').strip()
    pdf_file = request.files.get('paper_pdf')

    if not pdf_file or not allowed_file(pdf_file.filename):
        flash("Please upload a valid PDF file.", "warning")
        return redirect(url_for('admin_manage_subject', subject_id=subject.id))

    safe_name = secure_filename(pdf_file.filename)
    unique_filename = f"{subject.code}_{session_name.replace(' ', '_')}_{safe_name}"
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
    pdf_file.save(save_path)

    try:
        reader = PdfReader(save_path)
        extracted_text = "\n".join([page.extract_text() or '' for page in reader.pages])
    except Exception:
        extracted_text = ""

    parsed_json_str = None
    api_key = os.environ.get('GEMINI_API_KEY')

    if api_key and extracted_text.strip():
        system_instruction = (
            "Extract all questions from this official GTU engineering examination paper. "
            "Return ONLY a clean JSON array representing questions (Q.1 to Q.5):\n"
            "[\n"
            "  {\n"
            "    \"q_no\": \"Q.1\",\n"
            "    \"total_marks\": 14,\n"
            "    \"parts\": [{\"sub\": \"(a)\", \"text\": \"Question text\", \"marks\": 3}],\n"
            "    \"or_parts\": []\n"
            "  }\n"
            "]\n"
            "Do NOT include markdown like ```json. Return raw JSON only."
        )

        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
        payload = {
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"parts": [{"text": extracted_text[:14000]}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
        }

        try:
            res = requests.post(url, headers=headers, json=payload, timeout=60)
            if res.status_code == 200:
                raw_out = res.json()['candidates'][0]['content']['parts'][0]['text']
                parsed_json_str = clean_gemini_json(raw_out)
        except Exception as e:
            print("AI mock parsing failed on upload:", e)

    paper = ExamPaper(
        subject_id=subject.id,
        session_name=session_name,
        pdf_filename=unique_filename,
        paper_json=parsed_json_str
    )
    db.session.add(paper)
    db.session.commit()

    if parsed_json_str:
        flash(f"Exam paper for {session_name} uploaded and mock test ready!", "success")
    else:
        flash(f"Paper uploaded as PDF only. You can generate the mock test from admin.", "info")

    return redirect(url_for('admin_manage_subject', subject_id=subject.id))

# NEW: Route to generate Mock Test JSON for papers already uploaded as PDF only
@app.route('/admin/paper/<int:paper_id>/extract-mock', methods=['POST'])
@login_required
def extract_mock_questions(paper_id):
    if not current_user.is_admin:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    paper = ExamPaper.query.get_or_404(paper_id)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], paper.pdf_filename)

    if not os.path.exists(file_path):
        flash("PDF file not found on disk.", "danger")
        return redirect(url_for('admin_manage_subject', subject_id=paper.subject_id))

    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        flash("GEMINI_API_KEY is not set.", "danger")
        return redirect(url_for('admin_manage_subject', subject_id=paper.subject_id))

    try:
        reader = PdfReader(file_path)
        extracted_text = "\n".join([page.extract_text() or '' for page in reader.pages])

        if not extracted_text.strip():
            flash("Could not read text from this PDF file (scanned image).", "warning")
            return redirect(url_for('admin_manage_subject', subject_id=paper.subject_id))

        system_instruction = (
            "Extract all questions from this official GTU engineering examination paper. "
            "Return ONLY a clean JSON array representing questions (Q.1 to Q.5):\n"
            "[\n"
            "  {\n"
            "    \"q_no\": \"Q.1\",\n"
            "    \"total_marks\": 14,\n"
            "    \"parts\": [{\"sub\": \"(a)\", \"text\": \"Question text\", \"marks\": 3}],\n"
            "    \"or_parts\": []\n"
            "  }\n"
            "]\n"
            "Do NOT include markdown like ```json. Return raw JSON only."
        )

# ✅ CORRECT (clean standard URL)
        url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
        payload = {
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"parts": [{"text": extracted_text[:14000]}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}
        }

        res = requests.post(url, headers=headers, json=payload, timeout=60)
        resp_data = res.json()

        if res.status_code != 200:
            flash(f"Gemini API error: {resp_data.get('error', {}).get('message')}", "danger")
            return redirect(url_for('admin_manage_subject', subject_id=paper.subject_id))

        raw_out = resp_data['candidates'][0]['content']['parts'][0]['text']
        cleaned_out = clean_gemini_json(raw_out)
        
        # Verify valid JSON
        json.loads(cleaned_out)

        paper.paper_json = cleaned_out
        db.session.commit()
        flash(f"Mock exam questions extracted successfully for {paper.session_name}!", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Error parsing paper: {str(e)}", "danger")

    return redirect(url_for('admin_manage_subject', subject_id=paper.subject_id))

@app.route('/api/gtu-ai-tutor', methods=['POST'])
@login_required
def gtu_ai_tutor():
    data = request.get_json() or {}
    question = data.get('question', '').strip()
    marks = str(data.get('marks', '7'))
    subject_name = data.get('subject_name', 'Engineering')
    subject_id = data.get('subject_id')

    if not question:
        return jsonify({'error': 'Please provide a question.'}), 400

    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        return jsonify({'error': 'GEMINI_API_KEY environment variable is not set.'}), 500

    system_instruction = (
        f"You are a friendly, experienced GTU engineering professor and paper evaluator for {subject_name}. "
        f"Explain the answer for a {marks}-Mark GTU question in clear, simple, student-friendly language. "
        "Your explanation should be effortless to memorize, understand, and write in an exam paper.\n\n"
        "CRITICAL FORMATTING RULES:\n"
        "- NEVER use LaTeX, dollar signs ($ or $$), or backslash symbols like \\rightarrow, \\le, or \\mathbf.\n"
        "- Write all variables, arrays, and math in standard plain text (e.g., write 'A[i]', 'i = -1', 'n log n', 'O(n^2)', '->' instead of LaTeX).\n"
        "- Keep the language simple, direct, and conversational—avoid overly dense or academic jargon.\n\n"
        "Structure the answer with these clear sections:\n"
        "1. **Core Concept & Simple Definition**: Explain what it is and why we use it in plain English with an everyday analogy if helpful.\n"
        "2. **Neat Diagram / Flow**: A simple, clean ASCII diagram showing how the data or process moves.\n"
        "3. **Step-by-Step Algorithm**: Clear, numbered steps using straightforward logic.\n"
        "4. **Hand-Trace with a Small Example**: Take a small array/input and show step-by-step what changes at each stage in a clean, readable text table.\n"
        "5. **Time & Space Complexity**: Clearly state Best, Average, and Worst Case (e.g., O(n log n)) with a 1-line reason for each.\n"
        "6. **Where to Use & Key Advantages**: 3 to 4 quick bullet points.\n"
        "Make it direct, clean, and completely free of confusing symbols."
    )
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key
    }
    payload = {
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "contents": [
            {"parts": [{"text": f"Subject: {subject_name}\nQuestion: {question}\nTarget Marks: {marks} Marks (Provide an exhaustive, high-scoring university answer)"}]}
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4000
        }
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=90)
        resp_data = response.json()

        if response.status_code != 200:
            error_msg = resp_data.get('error', {}).get('message', 'Failed to generate answer.')
            return jsonify({'error': error_msg}), response.status_code

        answer = resp_data['candidates'][0]['content']['parts'][0]['text']

        log = ActivityLog(
            user_id=current_user.id,
            action_type='ai_query',
            subject_id=subject_id
        )
        db.session.add(log)
        db.session.commit()

        return jsonify({'status': 'success', 'answer': answer})

    except requests.exceptions.Timeout:
        return jsonify({'error': 'The server took too long to generate the full academic answer. Please try again.'}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f"Network error contacting Gemini API: {str(e)}"}), 500

@app.route('/admin/analytics')
@login_required
def admin_analytics():
    if not current_user.is_admin:
        flash("Unauthorized.", "danger")
        return redirect(url_for('dashboard'))

    total_students = User.query.filter_by(is_admin=False).count()
    total_logins = ActivityLog.query.filter_by(action_type='login').count()
    total_ai_queries = ActivityLog.query.filter_by(action_type='ai_query').count()

    subject_stats = db.session.query(
        Subject.name,
        Subject.code,
        func.count(ActivityLog.id).label('engagement_count')
    ).outerjoin(ActivityLog, Subject.id == ActivityLog.subject_id)\
     .group_by(Subject.id)\
     .order_by(func.count(ActivityLog.id).desc()).all()

    subject_labels = [s[0] for s in subject_stats]
    subject_data = [s[2] for s in subject_stats]

    branch_stats = db.session.query(
        User.department,
        func.count(User.id)
    ).filter_by(is_admin=False).group_by(User.department).all()

    branch_labels = [b[0] if b[0] else 'General' for b in branch_stats]
    branch_data = [b[1] for b in branch_stats]

    return render_template(
        'admin/admin_analytics.html',
        total_students=total_students,
        total_logins=total_logins,
        total_ai_queries=total_ai_queries,
        subject_stats=subject_stats,
        subject_labels=subject_labels,
        subject_data=subject_data,
        branch_labels=branch_labels,
        branch_data=branch_data
    )

@app.route('/admin/export/monthly-report')
@login_required
def export_monthly_report():
    if not current_user.is_admin:
        return "Unauthorized", 403

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Activity ID', 'Enrollment No', 'Student Name', 'Action Type', 'Subject', 'Timestamp (UTC)'])

    logs = ActivityLog.query.order_by(ActivityLog.timestamp.desc()).limit(1000).all()
    for log in logs:
        writer.writerow([
            log.id,
            log.user.enrollment_no if log.user else 'Unknown',
            log.user.name if log.user else 'Unknown',
            log.action_type,
            log.subject.name if log.subject else 'General App',
            log.timestamp.strftime('%Y-%m-%d %H:%M:%S')
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=gtu_prep_monthly_report.csv"}
    )

# ----------------- SEED DATA -----------------

def seed_gtu_data():
    admin_user = User.query.filter_by(enrollment_no="admin").first()
    if not admin_user:
        admin_user = User(
            enrollment_no="admin",
            name="GTU Admin",
            department="Computer Engineering",
            semester=7,
            is_admin=True,
            password_hash=generate_password_hash("admin123")
        )
        db.session.add(admin_user)
        db.session.commit()
        print("Admin account created successfully!")

    if Subject.query.first():
        return

    ada = Subject(code="3150703", name="Analysis and Design of Algorithms", department="Computer Engineering", semester=5)
    db.session.add(ada)
    db.session.flush()

    topics_ada = [
        Topic(subject_id=ada.id, unit_no=1, title="Algorithm Analysis & Asymptotic Notations", weightage=15),
        Topic(subject_id=ada.id, unit_no=2, title="Divide and Conquer: Sorting & Searching", weightage=20),
        Topic(subject_id=ada.id, unit_no=3, title="Greedy Approach: Knapsack & Spanning Trees", weightage=25),
        Topic(subject_id=ada.id, unit_no=4, title="Dynamic Programming: 0/1 Knapsack & LCS", weightage=25),
        Topic(subject_id=ada.id, unit_no=5, title="Backtracking: N-Queens & TSP", weightage=15),
    ]
    db.session.bulk_save_objects(topics_ada)

    cd = Subject(code="3170701", name="Compiler Design", department="Computer Engineering", semester=7)
    db.session.add(cd)
    db.session.flush()

    topics_cd = [
        Topic(subject_id=cd.id, unit_no=1, title="Overview of Translation & Lexical Analysis", weightage=20),
        Topic(subject_id=cd.id, unit_no=2, title="Parsing Techniques (LL, LR, LALR)", weightage=30),
        Topic(subject_id=cd.id, unit_no=3, title="Syntax Directed Translation & Intermediate Code", weightage=25),
        Topic(subject_id=cd.id, unit_no=4, title="Code Generation and Optimization", weightage=25),
    ]
    db.session.bulk_save_objects(topics_cd)

    it_sub = Subject(code="3151601", name="Web Development Essentials", department="Information Technology", semester=5)
    db.session.add(it_sub)
    db.session.flush()

    topics_it = [
        Topic(subject_id=it_sub.id, unit_no=1, title="HTML5, CSS3 and Responsive Grids", weightage=25),
        Topic(subject_id=it_sub.id, unit_no=2, title="Client-Side Scripting with JavaScript", weightage=35),
        Topic(subject_id=it_sub.id, unit_no=3, title="Backend Architecture & RESTful APIs", weightage=40),
    ]
    db.session.bulk_save_objects(topics_it)
    db.session.commit()

with app.app_context():
        db.create_all()
        seed_gtu_data()

if __name__ == '__main__':
    app.run(debug=True)