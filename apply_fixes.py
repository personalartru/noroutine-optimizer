import os

def apply_fixes():
    print("Начинаю применение исправлений...")
    
    # ==========================================
    # 1. Правки в max_bot.py
    # ==========================================
    if os.path.exists("max_bot.py"):
        with open("max_bot.py", "r", encoding="utf-8") as f:
            content = f.read()
        
        original_content = content
        
        # 1.1 Добавляем import tempfile
        if "import tempfile" not in content:
            content = content.replace("import requests", "import requests\nimport tempfile")
            
        # 1.2 Заменяем save_json на атомарную версию
        old_save_json = '''def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return True
    except OSError as e:
        print(f"Не удалось сохранить {path}:", e)
        return False'''
        
        new_save_json = '''def save_json(path, data):
    """Атомарное сохранение JSON: защита от повреждения при обрыве записи."""
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=dir_name, text=True, encoding="utf-8")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(temp_path, path)
        return True
    except OSError as e:
        print(f"Не удалось сохранить {path}:", e)
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return False'''
        content = content.replace(old_save_json, new_save_json)

        # 1.3 Заменяем save_text на атомарную версию
        old_save_text = '''def save_text(path, text):
    """Как save_json, только уже готовый текст: скачанный файл не пересобираем."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except OSError as e:
        print(f"Не удалось сохранить {path}:", e)
        return False'''
        
        new_save_text = '''def save_text(path, text):
    """Атомарное сохранение текста: защита от повреждения при обрыве записи."""
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=dir_name, text=True, encoding="utf-8")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(temp_path, path)
        return True
    except OSError as e:
        print(f"Не удалось сохранить {path}:", e)
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return False'''
        content = content.replace(old_save_text, new_save_text)

        # 1.4 Заменяем save_offset
        old_save_offset = '''def save_offset(offset):
    try:
        with open(OFFSET_FILE, "w", encoding="utf-8") as f:
            f.write(str(offset))
    except OSError:
        pass'''
        
        new_save_offset = '''def save_offset(offset):
    """Атомарное сохранение offset через save_text."""
    try:
        save_text(OFFSET_FILE, str(offset))
    except OSError:
        pass'''
        content = content.replace(old_save_offset, new_save_offset)

        # 1.5 Заменяем UPLOADS = {} на класс с ограничением памяти
        content = content.replace("UPLOADS = {}", '''class _BoundedCache:
    """Простой кэш с ограничением размера (LRU-like) для предотвращения утечек памяти."""
    def __init__(self, max_size=50):
        self.max_size = max_size
        self._cache = {}

    def get(self, key):
        return self._cache.get(key)

    def set(self, key, value):
        if len(self._cache) >= self.max_size:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = value

UPLOADS = _BoundedCache(max_size=50)''')

        # 1.6 Обновляем использование UPLOADS в функции upload_file
        content = content.replace("if key in UPLOADS:\n        return UPLOADS[key]", "if UPLOADS.get(key):\n        return UPLOADS.get(key)")
        content = content.replace("UPLOADS[key] = file_token", "UPLOADS.set(key, file_token)")

        # 1.7 Уменьшаем таймаут в fetch_content с 15 до 5 секунд
        content = content.replace("timeout=15", "timeout=5")
        content = content.replace("content.json не скачался:", "content.json не скачался (сеть/таймаут):")

        if content != original_content:
            with open("max_bot.py", "w", encoding="utf-8") as f:
                f.write(content)
            print("✅ max_bot.py успешно обновлён.")
        else:
            print("⚠️ В max_bot.py не найдены ожидаемые фрагменты. Возможно, файл уже изменён.")

    # ==========================================
    # 2. Правки в optimizer.py
    # ==========================================
    if os.path.exists("optimizer.py"):
        with open("optimizer.py", "r", encoding="utf-8") as f:
            content = f.read()
            
        original_content = content
        
        # 2.1 Добавляем проверку MAX_HEADCOUNT в dispatch_text
        old_block = '''        match = re.search(r"\\d+", text or "")
        if not match:
            return session, emit(content.text("errors.need_number"))
        record_role(session, role["label"], int(match.group()), role["rate"])'''
        
        new_block = '''        match = re.search(r"\\d+", text or "")
        if not match:
            return session, emit(content.text("errors.need_number"))
        
        count = int(match.group())
        if count > MAX_HEADCOUNT:
            return session, emit(
                f"Максимальное количество для расчёта — {MAX_HEADCOUNT} человек. "
                f"Пожалуйста, укажите число меньше или свяжитесь с нами для корпоративного расчёта."
            )
            
        record_role(session, role["label"], count, role["rate"])'''
        
        content = content.replace(old_block, new_block)

        if content != original_content:
            with open("optimizer.py", "w", encoding="utf-8") as f:
                f.write(content)
            print("✅ optimizer.py успешно обновлён.")
        else:
            print("⚠️ В optimizer.py не найдены ожидаемые фрагменты.")

    print("\n🎉 Готово! Теперь вы можете закоммитить и отправить изменения:")
    print("git add max_bot.py optimizer.py")
    print('git commit -m "fix: атомарная запись, ограничение кэша и валидация headcount"')
    print("git push")

if __name__ == "__main__":
    apply_fixes()
