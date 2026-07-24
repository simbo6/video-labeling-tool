# -*- coding: utf-8 -*-
import hashlib
import os
import sqlite3
from datetime import datetime


class AppDatabase:
    def __init__(self, db_path=None):
        """初始化 SQLite 数据库连接，并确保基础表结构存在。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.db_path = db_path or os.path.join(root, "data", "app.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self):
        """创建项目、媒体、标注类型、标注记录和用户表。"""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS media_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '进行中',
                patient_name TEXT,
                patient_age TEXT,
                patient_gender TEXT,
                hospital TEXT,
                dominant_hand TEXT,
                birth_date TEXT,
                seizure_date TEXT,
                wearing_hand TEXT,
                video_start TEXT,
                video_end TEXT,
                video_note TEXT,
                eeg_marker_start TEXT,
                eeg_marker_end TEXT,
                watch_start TEXT,
                watch_end TEXT,
                acc_fs REAL DEFAULT 100,
                gyro_fs REAL DEFAULT 100,
                emg_fs REAL DEFAULT 200,
                ppg_fs REAL DEFAULT 25,
                video_path TEXT,
                watch_path TEXT,
                hospital_type TEXT,
                hospital_data_path TEXT,
                signal_offset_ms INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS annotation_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS annotations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER,
                label TEXT NOT NULL,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL,
                video_path TEXT,
                watch_path TEXT,
                patient_name TEXT,
                patient_age TEXT,
                patient_gender TEXT,
                hospital TEXT,
                dominant_hand TEXT,
                birth_date TEXT,
                seizure_date TEXT,
                wearing_hand TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_login_at TEXT
            );
            """
        )
        self.conn.commit()
        self.ensure_schema_columns()
        self.normalize_default_values()
        if not self.list_annotation_types():
            self.add_annotation_type("癫痫")
        if not self.has_users():
            self.create_user("admin", "admin")

    def ensure_schema_columns(self):
        """为旧数据库补齐新版本需要的字段。"""
        annotation_columns = {
            "project_id": "INTEGER",
            "patient_name": "TEXT",
            "patient_age": "TEXT",
            "patient_gender": "TEXT",
            "hospital": "TEXT",
            "dominant_hand": "TEXT",
            "birth_date": "TEXT",
            "seizure_date": "TEXT",
            "wearing_hand": "TEXT",
        }
        project_columns = {
            "status": "TEXT DEFAULT '进行中'",
            "patient_name": "TEXT",
            "patient_age": "TEXT",
            "patient_gender": "TEXT",
            "hospital": "TEXT",
            "dominant_hand": "TEXT",
            "birth_date": "TEXT",
            "seizure_date": "TEXT",
            "wearing_hand": "TEXT",
            "video_start": "TEXT",
            "video_end": "TEXT",
            "video_note": "TEXT",
            "eeg_marker_start": "TEXT",
            "eeg_marker_end": "TEXT",
            "watch_start": "TEXT",
            "watch_end": "TEXT",
            "acc_fs": "REAL DEFAULT 100",
            "gyro_fs": "REAL DEFAULT 100",
            "emg_fs": "REAL DEFAULT 200",
            "ppg_fs": "REAL DEFAULT 25",
            "video_path": "TEXT",
            "watch_path": "TEXT",
            "hospital_type": "TEXT",
            "hospital_data_path": "TEXT",
            "signal_offset_ms": "INTEGER DEFAULT 0",
            "created_at": "TEXT",
            "updated_at": "TEXT",
        }
        for column, definition in annotation_columns.items():
            self.ensure_column("annotations", column, definition)
        for column, definition in project_columns.items():
            self.ensure_column("projects", column, definition)

    def ensure_column(self, table, column, definition):
        """如果指定表缺少某列，则执行 ALTER TABLE 追加字段。"""
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = {row["name"] for row in rows}
        if column not in names:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self.conn.commit()

    def normalize_default_values(self):
        """把旧版本乱码默认值修正为正常中文。"""
        self.conn.execute("UPDATE OR IGNORE annotation_types SET name = ? WHERE name LIKE ?", ("癫痫", "%棲%"))
        self.conn.execute("DELETE FROM annotation_types WHERE name LIKE ?", ("%棲%",))
        self.conn.execute("UPDATE annotations SET label = ? WHERE label LIKE ?", ("癫痫", "%棲%"))
        self.conn.execute("UPDATE projects SET status = ? WHERE status IS NULL OR status = '' OR status LIKE ?", ("进行中", "%?%"))
        self.conn.commit()

    def now(self):
        """返回数据库统一使用的当前时间字符串。"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def set_state(self, key, value):
        """保存一个应用级键值状态。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO app_state(key, value) VALUES(?, ?)",
            (key, value or "")
        )
        self.conn.commit()

    def get_state(self, key, default=""):
        """读取一个应用级键值状态。"""
        row = self.conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def save_media_file(self, kind, path):
        """保存最近导入的视频或腕表数据文件路径。"""
        if not path:
            return
        self.conn.execute(
            """
            INSERT OR REPLACE INTO media_files(kind, path, name, created_at)
            VALUES(?, ?, ?, ?)
            """,
            (kind, path, os.path.basename(path), self.now())
        )
        self.set_state(f"{kind}_path", path)
        self.conn.commit()

    def list_media_files(self, kind=None):
        """按类型列出导入过的媒体文件。"""
        if kind:
            rows = self.conn.execute(
                "SELECT * FROM media_files WHERE kind = ? ORDER BY created_at DESC",
                (kind,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM media_files ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def create_project(self, name=None):
        """创建新项目，并把它设置为当前项目。"""
        now = self.now()
        project_name = name or f"项目 {now}"
        cursor = self.conn.execute(
            """
            INSERT INTO projects(name, status, created_at, updated_at)
            VALUES(?, ?, ?, ?)
            """,
            (project_name, "进行中", now, now)
        )
        self.conn.commit()
        project_id = cursor.lastrowid
        self.set_state("current_project_id", str(project_id))
        return self.get_project(project_id)

    def get_project(self, project_id):
        """按项目 ID 读取项目详情。"""
        if not project_id:
            return None
        row = self.conn.execute("SELECT * FROM projects WHERE id = ?", (int(project_id),)).fetchone()
        return dict(row) if row is not None else None

    def get_current_project(self):
        """读取当前项目；如果不存在则创建默认项目。"""
        project_id = self.get_state("current_project_id", "")
        project = self.get_project(project_id) if project_id else None
        if project is None:
            project = self.create_project("默认项目")
        return project

    def set_current_project(self, project_id):
        """把指定项目设置为当前项目。"""
        if self.get_project(project_id) is not None:
            self.set_state("current_project_id", str(project_id))

    def list_projects(self):
        """按更新时间倒序列出所有项目。"""
        rows = self.conn.execute("SELECT * FROM projects ORDER BY updated_at DESC, id DESC").fetchall()
        return [dict(row) for row in rows]

    def update_project(self, project_id, **fields):
        """更新项目字段，并刷新更新时间。"""
        allowed = {
            "name", "status", "patient_name", "patient_age", "patient_gender", "hospital",
            "dominant_hand", "birth_date", "seizure_date", "wearing_hand",
            "video_start", "video_end", "video_note", "eeg_marker_start", "eeg_marker_end",
            "watch_start", "watch_end", "acc_fs", "gyro_fs", "emg_fs", "ppg_fs",
            "video_path", "watch_path", "hospital_type", "hospital_data_path", "signal_offset_ms",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return self.get_project(project_id)
        values["updated_at"] = self.now()
        assignments = ", ".join([f"{key} = ?" for key in values])
        params = list(values.values()) + [int(project_id)]
        self.conn.execute(f"UPDATE projects SET {assignments} WHERE id = ?", params)
        self.conn.commit()
        return self.get_project(project_id)

    def delete_project(self, project_id):
        """删除项目及其标注记录。"""
        self.conn.execute("DELETE FROM annotations WHERE project_id = ?", (int(project_id),))
        self.conn.execute("DELETE FROM projects WHERE id = ?", (int(project_id),))
        self.conn.commit()
        current = self.get_state("current_project_id", "")
        if str(project_id) == str(current):
            projects = self.list_projects()
            if projects:
                self.set_state("current_project_id", str(projects[0]["id"]))
            else:
                self.create_project("默认项目")

    def list_annotation_types(self):
        """列出所有标注类型。"""
        rows = self.conn.execute("SELECT name FROM annotation_types ORDER BY id").fetchall()
        names = [row["name"] for row in rows]
        return names or ["癫痫"]

    def add_annotation_type(self, name):
        """新增标注类型，重复名称会被忽略。"""
        self.conn.execute(
            "INSERT OR IGNORE INTO annotation_types(name, created_at) VALUES(?, ?)",
            (name, self.now())
        )
        self.conn.commit()

    def rename_annotation_type(self, old_name, new_name):
        """重命名标注类型，并同步历史标注记录中的标签。"""
        self.conn.execute("UPDATE annotation_types SET name = ? WHERE name = ?", (new_name, old_name))
        self.conn.execute("UPDATE annotations SET label = ? WHERE label = ?", (new_name, old_name))
        self.conn.commit()

    def delete_annotation_type(self, name):
        """删除一个标注类型。"""
        self.conn.execute("DELETE FROM annotation_types WHERE name = ?", (name,))
        self.conn.commit()

    def save_annotation(self, label, start_ms, end_ms, video_path="", watch_path="", project_id=None, project_info=None):
        """保存一条标注记录，并冗余保存当时的患者信息便于导出。"""
        project_info = project_info or {}
        cursor = self.conn.execute(
            """
            INSERT INTO annotations(
                project_id, label, start_ms, end_ms, video_path, watch_path,
                patient_name, patient_age, patient_gender, hospital,
                dominant_hand, birth_date, seizure_date, wearing_hand, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id, label, int(start_ms), int(end_ms), video_path or "", watch_path or "",
                project_info.get("patient_name", ""), project_info.get("patient_age", ""),
                project_info.get("patient_gender", ""), project_info.get("hospital", ""),
                project_info.get("dominant_hand", ""), project_info.get("birth_date", ""),
                project_info.get("seizure_date", ""), project_info.get("wearing_hand", ""), self.now()
            )
        )
        self.conn.commit()
        return cursor.lastrowid

    def delete_annotation(self, annotation_id):
        """删除指定标注记录。"""
        self.conn.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
        self.conn.commit()

    def list_annotations(self, project_id=None):
        """列出标注记录，并附带项目腕表开始时间用于真实时间显示。"""
        sql = """
            SELECT a.*, p.watch_start AS watch_start
            FROM annotations a
            LEFT JOIN projects p ON p.id = a.project_id
        """
        if project_id is None:
            rows = self.conn.execute(sql + " ORDER BY a.id").fetchall()
        else:
            rows = self.conn.execute(
                sql + " WHERE a.project_id = ? ORDER BY a.id",
                (int(project_id),)
            ).fetchall()
        return [dict(row) for row in rows]

    def make_password_hash(self, password, salt):
        """根据明文密码和盐值生成 SHA-256 哈希。"""
        value = (salt + password).encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    def has_users(self):
        """判断用户表是否已有账号。"""
        row = self.conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return row["count"] > 0

    def create_user(self, username, password):
        """创建登录用户。"""
        username = (username or "").strip()
        password = password or ""
        if not username or not password:
            return False, "用户名和密码不能为空"
        if self.get_user(username) is not None:
            return False, "用户已存在"
        salt = os.urandom(16).hex()
        password_hash = self.make_password_hash(password, salt)
        self.conn.execute(
            """
            INSERT INTO users(username, password_hash, salt, created_at)
            VALUES(?, ?, ?, ?)
            """,
            (username, password_hash, salt, self.now())
        )
        self.conn.commit()
        return True, "注册成功"

    def get_user(self, username):
        """按用户名读取用户。"""
        row = self.conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row is not None else None

    def verify_user(self, username, password):
        """校验登录密码，并记录最后登录时间。"""
        user = self.get_user((username or "").strip())
        if user is None:
            return False
        password_hash = self.make_password_hash(password or "", user["salt"])
        if password_hash != user["password_hash"]:
            return False
        self.conn.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (self.now(), user["id"])
        )
        self.conn.commit()
        return True

    def close(self):
        """关闭数据库连接。"""
        self.conn.close()
