#!/usr/bin/env python3
"""context_keeper — сохраняет контекст каждого проекта Claude Code в .claude/CONTEXT.md.

Зачем: сессии (транскрипты) живут в ~/.claude/projects/ и пропадают при удалении
сессии/проекта в приложении. Скрипт раз в день читает свежие транскрипты, через
`claude -p` сводит их с прежним CONTEXT.md и пишет обновлённую выжимку прямо в
рабочую папку проекта (+ резервная копия в ~/.claude/context-archive/).
Новая сессия в этой папке получает выжимку автоматически через хук SessionStart.

Команды:
  update [--project PATH] [--force] [--no-llm] [--dry-run]
      Обновить все проекты с новой активностью (или один проект).
  inject
      Для хука SessionStart: читает JSON из stdin, печатает CONTEXT.md текущей папки.
  status
      Показать проекты, даты последнего обновления и где лежат файлы.
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
KEEPER_DIR = CLAUDE_DIR / "context-keeper"
ARCHIVE_DIR = CLAUDE_DIR / "context-archive"
STATE_FILE = KEEPER_DIR / "state.json"
LOG_FILE = KEEPER_DIR / "keeper.log"
WORK_DIR = KEEPER_DIR / "work"  # cwd для `claude -p`, чтобы не засорять проекты

CONTEXT_REL = Path(".claude") / "CONTEXT.md"
GUARD_ENV = "CONTEXT_KEEPER_RUNNING"

MODEL = os.environ.get("CONTEXT_KEEPER_MODEL", "sonnet")
DIGEST_BUDGET = 150_000       # символов сырого материала на проект за один прогон
SESSION_BUDGET = 40_000       # символов на одну сессию
INJECT_LIMIT = 30_000         # сколько символов CONTEXT.md отдавать в новую сессию
STALE_HOURS = 26              # если выжимка старше — хук запускает фоновое обновление
LLM_TIMEOUT = 900

SKIP_CWD_PARTS = ("/scratch-workspaces/", "/.claude/context-keeper/")
WORKTREE_RE = re.compile(r"^(.*?)/\.claude/worktrees/[^/]+")
TAG_RE = re.compile(r"<(system-reminder|command-[a-z-]+|local-command-[a-z-]+)>.*?</\1>", re.S)


# ---------- утилиты ----------

def log(msg):
    KEEPER_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if sys.stderr.isatty():
        print(line, file=sys.stderr)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    KEEPER_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def project_root(cwd):
    """Сессии в worktree относятся к основному репозиторию."""
    m = WORKTREE_RE.match(cwd)
    return m.group(1) if m else cwd


def archive_path(root):
    slug = re.sub(r"[^\w.-]+", "_", root.strip("/")).strip("_") or "root"
    return ARCHIVE_DIR / f"{slug}.md"


def clip(text, n):
    text = text.strip()
    return text if len(text) <= n else text[:n] + f" …[+{len(text) - n} симв.]"


def read_jsonl(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def session_cwd(path):
    for d in read_jsonl(path):
        if d.get("cwd"):
            return d["cwd"]
    return None


# ---------- сбор проектов ----------

def discover_projects():
    """{root: [jsonl, ...]} — группировка транскриптов по реальной папке проекта."""
    projects = {}
    if not PROJECTS_DIR.is_dir():
        return projects
    for pdir in PROJECTS_DIR.iterdir():
        if not pdir.is_dir():
            continue
        for jf in pdir.glob("*.jsonl"):
            cwd = session_cwd(jf)
            if not cwd or any(p in cwd + "/" for p in SKIP_CWD_PARTS):
                continue
            root = project_root(cwd)
            if root == str(HOME) or root == "/":
                continue
            projects.setdefault(root, []).append(jf)
    return projects


def memory_dirs(root):
    """Папки авто-памяти Claude для проекта и его worktree."""
    out = []
    for pdir in PROJECTS_DIR.iterdir():
        mem = pdir / "memory"
        if not mem.is_dir():
            continue
        jfs = list(pdir.glob("*.jsonl"))
        cwd = session_cwd(jfs[0]) if jfs else None
        if cwd and project_root(cwd) == root:
            out.append(mem)
    return out


# ---------- выжимка из транскрипта ----------

def text_of(content):
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
    return "\n".join(parts)


def digest_session(path, since_ts):
    title, first_ts, last_ts = None, None, None
    lines, files = [], []
    for d in read_jsonl(path):
        t = d.get("type")
        if t == "custom-title":
            title = d.get("customTitle") or title
            continue
        if t not in ("user", "assistant") or d.get("isSidechain"):
            continue
        ts = d.get("timestamp")
        if since_ts and ts and ts < since_ts:
            continue
        first_ts = first_ts or ts
        last_ts = ts or last_ts
        msg = d.get("message") or {}
        content = msg.get("content")
        if t == "user":
            if d.get("isCompactSummary"):
                lines.append("[СВОДКА ПРЕДЫДУЩЕЙ ЧАСТИ СЕССИИ]\n" + clip(text_of(content), 6000))
                continue
            if d.get("isMeta") or d.get("toolUseResult") is not None:
                continue
            txt = TAG_RE.sub("", text_of(content)).strip()
            if txt:
                lines.append("ПОЛЬЗОВАТЕЛЬ: " + clip(txt, 2000))
        else:
            for b in content or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    lines.append("CLAUDE: " + clip(b["text"], 1200))
                elif b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if fp and b.get("name") in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                        files.append(fp)
    if not lines:
        return None, None
    head = f"### Сессия «{title or path.stem}» ({(first_ts or '')[:10]} … {(last_ts or '')[:10]})"
    body = "\n".join(lines)
    if len(body) > SESSION_BUDGET:  # начало + конец важнее середины
        half = SESSION_BUDGET // 2
        body = body[:half] + "\n…[середина сессии опущена]…\n" + body[-half:]
    if files:
        uniq = list(dict.fromkeys(files))
        body += "\nИЗМЕНЁННЫЕ ФАЙЛЫ: " + ", ".join(uniq[:60])
    return head + "\n" + body, last_ts


def build_digest(jsonls, since_ts):
    chunks = []
    for jf in sorted(jsonls, key=lambda p: p.stat().st_mtime, reverse=True):
        if since_ts and dt.datetime.fromtimestamp(jf.stat().st_mtime, dt.timezone.utc).isoformat() < since_ts:
            continue
        chunk, last = digest_session(jf, since_ts)
        if chunk:
            chunks.append((last or "", chunk))
    chunks.sort(key=lambda c: c[0])  # хронологически
    out, total = [], 0
    for _, ch in reversed(chunks):  # свежие в приоритете при нехватке бюджета
        if total + len(ch) > DIGEST_BUDGET:
            break
        out.append(ch)
        total += len(ch)
    return list(reversed(out))


# ---------- генерация CONTEXT.md ----------

PROMPT = """Ты ведёшь файл-память проекта для Claude Code. Его прочитает НОВАЯ сессия Claude, у которой
нет доступа к прежним переписке и памяти, — после чтения она должна работать так, будто продолжает
прежние сессии: знать цель проекта, текущее состояние, принятые решения и их причины, договорённости
и предпочтения пользователя, незакрытые задачи.

Проект: {root}
Сегодня: {today}

Ниже: (1) прежняя версия файла (может отсутствовать), (2) заметки авто-памяти Claude по проекту,
(3) выдержки из сессий, прошедших с прошлого обновления. Обнови файл: влей новое, исправь устаревшее,
убери то, что больше не актуально. Ничего полезного из прежней версии не теряй без причины.

Правила:
- Пиши по-русски, плотно и конкретно: пути файлов, команды, названия, даты (абсолютные).
- НИКОГДА не записывай секреты: пароли, токены, ключи API, номера карт. Только где они лежат.
- Факты из выдержек, не домыслы. Если что-то неясно — так и пометь.
- Объём до ~{max_words} слов. Выведи ТОЛЬКО содержимое файла в Markdown, без пояснений вокруг.

Структура:
# Контекст проекта: <название>
> Автовыжимка context_keeper · обновлено {today}. Не редактировать вручную раздел выше «Заметки вручную».

## Что это за проект
## Текущее состояние
## Ключевые решения (и почему)
## Как пользователь хочет, чтобы работали
## Открытые задачи и следующие шаги
## Важные файлы, команды, окружение
## Хронология (дата — тема сессии — итог; свежие сверху, до 25 строк)

=== (1) ПРЕЖНЯЯ ВЕРСИЯ ===
{previous}

=== (2) АВТО-ПАМЯТЬ ===
{memory}

=== (3) ВЫДЕРЖКИ ИЗ СЕССИЙ ===
{digest}
"""

MANUAL_MARK = "## Заметки вручную"


def split_manual(text):
    """Раздел «Заметки вручную» пользователь пишет сам — его сохраняем как есть."""
    i = text.find(MANUAL_MARK)
    return (text, "") if i < 0 else (text[:i].rstrip(), text[i:].strip())


def read_memory(root):
    parts = []
    for mem in memory_dirs(root):
        for f in sorted(mem.glob("*.md")):
            if f.name == "MEMORY.md":
                continue
            try:
                parts.append(f"--- {f.name}\n" + clip(f.read_text(encoding="utf-8"), 3000))
            except OSError:
                pass
    return "\n".join(parts) or "(нет)"


def run_llm(prompt):
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, **{GUARD_ENV: "1"})
    claude = (os.environ.get("CONTEXT_KEEPER_CLAUDE") or shutil.which("claude")
              or str(HOME / ".local" / "bin" / "claude"))
    cmd = [claude, "-p", "--model", MODEL, "--tools", "", "--no-session-persistence",
           "--output-format", "text"]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=WORK_DIR,
                       env=env, timeout=LLM_TIMEOUT)
    out = r.stdout.strip()
    if r.returncode != 0 or not out.startswith("#"):
        raise RuntimeError(f"claude -p rc={r.returncode}: {clip(r.stderr or out, 500)}")
    return out


def fallback_text(root, previous, digest, today):
    """Без LLM: прежний текст + список сессий с последними запросами."""
    lines = [previous.strip() or f"# Контекст проекта: {Path(root).name}", "",
             f"## Сырые заметки {today} (без LLM — сжать при следующем обновлении)"]
    for ch in digest:
        head, *rest = ch.split("\n")
        asks = [l for l in rest if l.startswith("ПОЛЬЗОВАТЕЛЬ:")][-3:]
        lines.append(f"- {head.lstrip('# ')}")
        lines += [f"  - {clip(a[13:], 300)}" for a in asks]
    return "\n".join(lines)


def ensure_git_exclude(root):
    """Не даём CONTEXT.md случайно попасть в коммит."""
    exclude = Path(root) / ".git" / "info" / "exclude"
    if not exclude.parent.is_dir():
        return
    entry = str(CONTEXT_REL)
    try:
        cur = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if entry not in cur.split("\n"):
            with open(exclude, "a", encoding="utf-8") as f:
                f.write(("" if cur.endswith("\n") or not cur else "\n") + entry + "\n")
    except OSError:
        pass


def update_project(root, jsonls, state, force=False, no_llm=False, dry_run=False):
    st = state.get(root, {})
    newest = max(j.stat().st_mtime for j in jsonls)
    if not force and st.get("source_mtime", 0) >= newest:
        return "без изменений"

    ctx_file = Path(root) / CONTEXT_REL
    arch = archive_path(root)
    previous = ""
    for src in (ctx_file, arch):
        if src.exists():
            previous = src.read_text(encoding="utf-8")
            break
    previous, manual = split_manual(previous)

    # Первый запуск — вся история (бюджет оставит самое свежее), дальше — только новое
    since = st.get("updated_at") if previous else None
    digest = build_digest(jsonls, since)
    if not digest and previous and not force:
        state[root] = {**st, "source_mtime": newest}
        return "нет новых сообщений"

    today = dt.date.today().isoformat()
    if dry_run:
        return f"dry-run: {len(digest)} сессий, {sum(map(len, digest))} симв."

    text, mode = None, "llm"
    if not no_llm:
        prompt = PROMPT.format(root=root, today=today, max_words=1800,
                               previous=previous or "(нет — первый запуск)",
                               memory=read_memory(root),
                               digest="\n\n".join(digest) or "(нет новых)")
        try:
            text = run_llm(prompt)
        except Exception as e:  # noqa: BLE001 — любой сбой LLM не должен терять материал
            log(f"LLM недоступен для {root}: {e}")
    if text is None:
        text, mode = fallback_text(root, previous, digest, today), "fallback"

    text = text.rstrip() + "\n\n" + (manual or f"{MANUAL_MARK}\n<!-- сюда можно дописывать своё: скрипт это сохраняет -->") + "\n"
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    arch.write_text(text, encoding="utf-8")
    if Path(root).is_dir():
        ctx_file.parent.mkdir(exist_ok=True)
        ctx_file.write_text(text, encoding="utf-8")
        ensure_git_exclude(root)
        where = str(ctx_file)
    else:
        where = f"{arch} (папки проекта нет)"

    state[root] = {"updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "source_mtime": newest, "mode": mode, "file": where}
    return f"обновлено ({mode}, {len(digest)} сессий) → {where}"


def cmd_update(args):
    if os.environ.get(GUARD_ENV):
        return
    lock = KEEPER_DIR / "update.lock"
    KEEPER_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        if lock.stat().st_mtime > dt.datetime.now().timestamp() - 3 * 3600:
            log("обновление уже идёт — пропуск")
            return
        lock.unlink()
        return cmd_update(args)
    try:
        state = load_state()
        projects = discover_projects()
        if args.project:
            want = project_root(str(Path(args.project).expanduser().resolve()))
            projects = {k: v for k, v in projects.items() if k == want}
            if not projects:
                log(f"нет сессий для {want}")
        for root, jsonls in sorted(projects.items()):
            try:
                res = update_project(root, jsonls, state, args.force, args.no_llm, args.dry_run)
            except Exception as e:  # noqa: BLE001
                res = f"ошибка: {e}"
            log(f"{root}: {res}")
            if not args.dry_run:
                save_state(state)
    finally:
        lock.unlink(missing_ok=True)


def cmd_inject(_args):
    if os.environ.get(GUARD_ENV):
        return
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    if data.get("source") == "compact":  # после авто-сжатия контекст и так в сессии
        return
    cwd = data.get("cwd") or os.getcwd()
    root = project_root(cwd)
    src = Path(root) / CONTEXT_REL
    if not src.exists():
        src = archive_path(root)
    if src.exists():
        text = src.read_text(encoding="utf-8")
        print(f"[context_keeper] Сохранённый контекст прошлых сессий этого проекта ({src}). "
              "Учитывай его как фон: это выжимка, сверяй с кодом, если что-то расходится.\n")
        print(clip(text, INJECT_LIMIT))

    # Подстраховка, если ежедневный запуск пропущен (ноутбук был выключен и т.п.)
    if any(p in root + "/" for p in SKIP_CWD_PARTS) or root in (str(HOME), "/"):
        return
    st = load_state().get(root, {})
    upd = st.get("updated_at")
    stale = not upd or dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(upd) > dt.timedelta(hours=STALE_HOURS)
    if stale:
        subprocess.Popen([sys.executable, __file__, "update", "--project", root],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def cmd_status(_args):
    state = load_state()
    for root in sorted(discover_projects()):
        st = state.get(root, {})
        print(f"{st.get('updated_at', 'никогда')[:16]:16}  {st.get('mode', '-'):8}  {root}")
    print(f"\nлог: {LOG_FILE}\nархив: {ARCHIVE_DIR}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update")
    u.add_argument("--project")
    u.add_argument("--force", action="store_true")
    u.add_argument("--no-llm", action="store_true")
    u.add_argument("--dry-run", action="store_true")
    sub.add_parser("inject")
    sub.add_parser("status")
    args = ap.parse_args()
    {"update": cmd_update, "inject": cmd_inject, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
