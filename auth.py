"""Server-side accounts, hashed passwords and revocable expiring sessions."""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SESSION_SECONDS=8*3600


class AuthenticationError(Exception):pass


@dataclass(frozen=True)
class Actor:
    username:str
    role:str

    @property
    def admin(self):return self.role=='admin'

    def public(self):return dict(username=self.username,role=self.role)


def username(value):
    if not isinstance(value,str):raise ValueError('用户名格式不正确')
    value=value.strip().lower()
    if not re.fullmatch(r'[a-z][a-z0-9_.-]{0,31}',value):raise ValueError('用户名须以字母开头，1–32位字母/数字/点/横线/下划线')
    return value


def password_hash(password,salt):
    return hashlib.scrypt(password.encode('utf-8'),salt=bytes.fromhex(salt),n=16384,r=8,p=1,dklen=32).hex()


def new_password(password):
    if not isinstance(password,str) or not 12<=len(password)<=256:raise ValueError('密码需为12–256个字符')
    salt=secrets.token_hex(16)
    return salt,password_hash(password,salt)


class Accounts:
    def __init__(self,directory,clock=time.time):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/'auth.sqlite';self.clock=clock
        try:
            fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        except FileExistsError:pass
        with self.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY,role TEXT NOT NULL,
                    salt TEXT NOT NULL,hash TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,username TEXT NOT NULL,
                    csrf TEXT NOT NULL,expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts(username TEXT PRIMARY KEY,failures INTEGER NOT NULL,
                    locked_until REAL NOT NULL);
            ''')
        if os.name=='posix':self.path.chmod(0o600)

    def connect(self):
        c=sqlite3.connect(self.path,timeout=10);c.row_factory=sqlite3.Row
        return c

    def add(self,name,password,role='user'):
        name=username(name)
        if role not in ('user','admin'):raise ValueError('角色只能为user/admin')
        salt,digest=new_password(password)
        with self.connect() as c:
            try:c.execute('INSERT INTO users VALUES(?,?,?,?,1)',(name,role,salt,digest))
            except sqlite3.IntegrityError:raise ValueError('用户名已存在') from None
        return dict(username=name,role=role)

    def list(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT username,role,enabled FROM users ORDER BY username')]

    def enabled(self,name):
        with self.connect() as c:return c.execute('SELECT 1 FROM users WHERE username=? AND enabled=1',(name,)).fetchone() is not None

    def reset_password(self,name,password):
        name=username(name);salt,digest=new_password(password)
        with self.connect() as c:
            if c.execute('UPDATE users SET salt=?,hash=? WHERE username=?',(salt,digest,name)).rowcount!=1:raise ValueError('用户不存在')
            c.execute('DELETE FROM sessions WHERE username=?',(name,))
            c.execute('DELETE FROM attempts WHERE username=?',(name,))

    def disable(self,name):
        name=username(name)
        with self.connect() as c:
            if c.execute('UPDATE users SET enabled=0 WHERE username=?',(name,)).rowcount!=1:raise ValueError('用户不存在')
            c.execute('DELETE FROM sessions WHERE username=?',(name,))

    def login(self,name,password):
        try:name=username(name)
        except ValueError:name=':invalid:'
        if not isinstance(password,str) or len(password)>256:raise AuthenticationError('用户名或密码错误')
        now=self.clock()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            attempt=c.execute('SELECT * FROM attempts WHERE username=?',(name,)).fetchone()
            if attempt and attempt['locked_until']>now:raise AuthenticationError('登录失败次数过多，请稍后再试')
            user=c.execute('SELECT * FROM users WHERE username=?',(name,)).fetchone()
            salt=user['salt'] if user else '00'*16
            actual=password_hash(password,salt)
            good=bool(user and user['enabled'] and hmac.compare_digest(actual,user['hash']))
            if not good:
                failures=(attempt['failures'] if attempt and attempt['locked_until']==0 else 0)+1
                locked_until=now+60 if failures>=5 else 0
                c.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?)',(name,failures,locked_until))
                c.commit()
                raise AuthenticationError('用户名或密码错误')
            c.execute('DELETE FROM attempts WHERE username=?',(name,))
            c.execute('DELETE FROM sessions WHERE expires<=?',(now,))
            token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(24);expires=now+SESSION_SECONDS
            c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(self.digest(token),name,csrf,expires))
        return dict(token=token,csrf=csrf,expires=expires,user=Actor(name,user['role']).public())

    @staticmethod
    def digest(token):return hashlib.sha256(token.encode('ascii')).hexdigest()

    def session(self,token,csrf=None):
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',token):raise AuthenticationError('请先登录')
        with self.connect() as c:
            row=c.execute('SELECT s.*,u.role,u.enabled FROM sessions s JOIN users u ON s.username=u.username WHERE s.hash=?',(self.digest(token),)).fetchone()
        if not row or not row['enabled'] or row['expires']<=self.clock():raise AuthenticationError('登录已过期，请重新登录')
        if csrf is not None and (not isinstance(csrf,str) or not hmac.compare_digest(row['csrf'],csrf)):raise PermissionError('CSRF校验失败')
        return Actor(row['username'],row['role']),dict(csrf=row['csrf'],expires=row['expires'])

    def logout(self,token):
        self.session(token)
        with self.connect() as c:c.execute('DELETE FROM sessions WHERE hash=?',(self.digest(token),))


def require_rc(store):
    if store.read().get('metadata',{}).get('multiuser_rc') is not True:
        raise ValueError('此目录不是多用户队列，拒绝接管现有运行数据')


def visible(task,actor):return actor.admin or task.get('owner_id')==actor.username


def allowed_task(value,task_id,actor):
    task=value['tasks'].get(task_id)
    if not task or not visible(task,actor):raise PermissionError('无权访问该任务')
    return task


def session_path(override=None):
    return Path(override or os.environ.get('GPU_QUEUE_SESSION',str(Path.home()/'.gpu-queue-panel-rc/session.json'))).expanduser()


def read_session(override=None):
    try:return __import__('json').loads(session_path(override).read_text(encoding='utf-8'))
    except (OSError,ValueError):raise AuthenticationError('请先运行users.py login登录多用户版') from None


def save_session(value,override=None):
    import json
    path=session_path(override);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+secrets.token_hex(8)+'.tmp')
    fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:json.dump(value,f)
    temporary.replace(path)
    return path
