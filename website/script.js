const examples = {
  events: {
    label: "INPUT / LISTENER LOG",
    html: '<p class="panel-kicker">Three events. One starting point.</p><div class="code-line"><time>09:41:02</time><span>Connection established</span></div><div class="code-line highlighted"><time>09:41:08</time><span>ORA-12514: service unknown</span></div><div class="code-line"><time>09:42:16</time><span>Service registration updated</span></div><p class="panel-footnote">The original events are the evidence.</p>',
  },
  context: {
    label: "PROCESS / CONNECT THE EVENTS",
    html: '<p class="panel-kicker">A signal, with its surroundings.</p><h3 class="panel-title">A connection error worth a look.</h3><p class="panel-body">A listener reported an unknown service. A registration update followed. Keep both events together for investigation, without assuming one explains the other.</p><span class="tag">ORA-12514</span> <span class="tag">Listener events</span>',
  },
  knowledge: {
    label: "OUTPUT / A GROWING WIKI",
    html: '<p class="panel-kicker">A starting point for next time.</p><h3 class="panel-title">The history stays within reach.</h3><p class="panel-body">The database journal preserves the sequence, links to the evidence, and connects the error to its wiki page. The next investigation starts with context.</p><span class="tag">Database journal</span> <span class="tag">Linked evidence ↗</span>',
  },
};
const tabs = [...document.querySelectorAll('[role="tab"]')];
function selectTab(tab) {
  tabs.forEach((item) => {
    item.setAttribute("aria-selected", String(item === tab));
    item.tabIndex = item === tab ? 0 : -1;
  });
  const example = examples[tab.dataset.step];
  document.getElementById("panel-label").textContent = example.label;
  document.getElementById("panel-content").innerHTML = example.html;
  document
    .getElementById("example-panel")
    .setAttribute("aria-labelledby", tab.id);
}
tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => selectTab(tab));
  tab.addEventListener("keydown", (event) => {
    let next;
    if (event.key === "ArrowDown") next = (index + 1) % tabs.length;
    if (event.key === "ArrowUp") next = (index - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = tabs.length - 1;
    if (next === undefined) return;
    event.preventDefault();
    selectTab(tabs[next]);
    tabs[next].focus();
  });
});
