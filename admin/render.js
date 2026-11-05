/* Подстановка переменных — одна на страницу и на тесты.

   Предпросмотр админки обязан совпадать с тем, что отдаёт `content.fill()` в
   Python: владелец в окне видит ровно то, что прочитает человек в чате. Поэтому
   функция живёт в отдельном файле — её же проверяет `tools/check_content.py`
   через node, сравнивая с кодом.

   Правила те же, что у `str.format`:
     {money}      — переменная; нет значения → оставляем как написано;
     двойные скобки — экранированные, подстановкой не считаются;
     {нет такой}  — остаётся в строке, страница помечает это замечанием.
*/
const VAR_TOKEN = /\{\{|\}\}|\{([a-z_][a-z0-9_]*)\}/g;

function renderTemplate(tpl, values) {
  return String(tpl || "").replace(VAR_TOKEN, (all, name) => {
    if (!name) return all === "}}" ? "}" : "{";
    return values && values[name] !== undefined ? String(values[name]) : all;
  });
}

/* Имена переменных в строке — в том же порядке, что их видит код. Двойные
   скобки сюда не попадают: для Python это не переменная, а экранированная скобка. */
function varNames(tpl) {
  const names = [];
  for (const match of String(tpl || "").matchAll(VAR_TOKEN)) {
    if (match[1]) names.push(match[1]);
  }
  return names;
}
