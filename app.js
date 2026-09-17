(function () {
  "use strict";

  var STORAGE_KEY = "discipline_v1";

  function dateKey(d) {
    var y = d.getFullYear();
    var m = String(d.getMonth() + 1).padStart(2, "0");
    var day = String(d.getDate()).padStart(2, "0");
    return y + "-" + m + "-" + day;
  }

  function formatLabel(key) {
    var d = new Date(key + "T00:00:00");
    return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  }

  function todayKey() {
    return dateKey(new Date());
  }

  function tomorrowKey() {
    var d = new Date();
    d.setDate(d.getDate() + 1);
    return dateKey(d);
  }

  function load() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : { days: {} };
    } catch (e) {
      return { days: {} };
    }
  }

  function save(data) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
  }

  var data = load();

  function getTasks(key) {
    return data.days[key] || [];
  }

  // Timed tasks sort chronologically first; untimed tasks keep their
  // add order and fall after all timed ones.
  function sortedTasks(key) {
    return getTasks(key)
      .map(function (t, i) { return { t: t, i: i }; })
      .sort(function (a, b) {
        if (a.t.time && b.t.time) return a.t.time < b.t.time ? -1 : a.t.time > b.t.time ? 1 : a.i - b.i;
        if (a.t.time) return -1;
        if (b.t.time) return 1;
        return a.i - b.i;
      })
      .map(function (x) { return x.t; });
  }

  function formatTime(time) {
    if (!time) return "";
    var parts = time.split(":");
    var d = new Date();
    d.setHours(Number(parts[0]), Number(parts[1]), 0, 0);
    return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }

  function addTask(key, text, time) {
    if (!data.days[key]) data.days[key] = [];
    data.days[key].push({
      id: String(Date.now()) + Math.random().toString(36).slice(2),
      text: text,
      time: time || null,
      done: false
    });
    save(data);
  }

  function toggleTask(key, id) {
    var tasks = getTasks(key);
    var task = tasks.find(function (t) { return t.id === id; });
    if (task) {
      task.done = !task.done;
      save(data);
    }
  }

  function deleteTask(key, id) {
    if (!data.days[key]) return;
    data.days[key] = data.days[key].filter(function (t) { return t.id !== id; });
    save(data);
  }

  // Streak: consecutive fully-completed days counting back from yesterday.
  // Today is excluded because it isn't finished yet.
  function computeStreak() {
    var streak = 0;
    var cursor = new Date();
    cursor.setDate(cursor.getDate() - 1);
    while (true) {
      var key = dateKey(cursor);
      var tasks = getTasks(key);
      if (tasks.length === 0) break;
      var allDone = tasks.every(function (t) { return t.done; });
      if (!allDone) break;
      streak++;
      cursor.setDate(cursor.getDate() - 1);
    }
    return streak;
  }

  function renderList(listEl, emptyEl, key, showCheckbox) {
    var tasks = sortedTasks(key);
    listEl.innerHTML = "";
    emptyEl.style.display = tasks.length === 0 ? "block" : "none";

    tasks.forEach(function (task) {
      var li = document.createElement("li");
      li.className = "task-item" + (task.done ? " done" : "");

      if (showCheckbox) {
        var checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = task.done;
        checkbox.addEventListener("change", function () {
          toggleTask(key, task.id);
          render();
        });
        li.appendChild(checkbox);
      }

      var timeEl = document.createElement("span");
      timeEl.className = "task-time";
      timeEl.textContent = formatTime(task.time);
      li.appendChild(timeEl);

      var span = document.createElement("span");
      span.className = "task-text";
      span.textContent = task.text;
      li.appendChild(span);

      var del = document.createElement("button");
      del.className = "task-delete";
      del.textContent = "remove";
      del.addEventListener("click", function () {
        deleteTask(key, task.id);
        render();
      });
      li.appendChild(del);

      listEl.appendChild(li);
    });
  }

  function render() {
    var tKey = todayKey();
    var pKey = tomorrowKey();

    document.getElementById("today-date").textContent = formatLabel(tKey);
    document.getElementById("plan-date").textContent = "For " + formatLabel(pKey);

    var todayTasks = getTasks(tKey);
    var doneCount = todayTasks.filter(function (t) { return t.done; }).length;
    document.getElementById("today-progress").textContent =
      todayTasks.length === 0 ? "" : doneCount + " of " + todayTasks.length + " done";

    renderList(document.getElementById("today-list"), document.getElementById("today-empty"), tKey, true);
    renderList(document.getElementById("plan-list"), document.getElementById("plan-empty"), pKey, false);

    var streak = computeStreak();
    document.getElementById("streak").textContent = streak > 0 ? streak + " day streak" : "";
  }

  function setupTabs() {
    var buttons = document.querySelectorAll(".tab-btn");
    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        buttons.forEach(function (b) { b.classList.remove("active"); });
        document.querySelectorAll(".view").forEach(function (v) { v.classList.remove("active"); });
        btn.classList.add("active");
        document.getElementById("view-" + btn.dataset.view).classList.add("active");
      });
    });
  }

  function setupPlanForm() {
    var form = document.getElementById("plan-form");
    var input = document.getElementById("plan-input");
    var timeInput = document.getElementById("plan-time");
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var text = input.value.trim();
      if (!text) return;
      addTask(tomorrowKey(), text, timeInput.value);
      input.value = "";
      timeInput.value = "";
      render();
    });
  }

  // Re-render on date rollover if the page is left open across midnight.
  function watchForDateChange() {
    var currentKey = todayKey();
    setInterval(function () {
      var key = todayKey();
      if (key !== currentKey) {
        currentKey = key;
        render();
      }
    }, 60000);
  }

  setupTabs();
  setupPlanForm();
  watchForDateChange();
  render();
})();
