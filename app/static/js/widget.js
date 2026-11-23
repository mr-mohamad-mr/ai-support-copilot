/* AI Support Copilot — embeddable chat widget (widget.js).
   Drop-in snippet for a tenant's own website:

     <script src="https://copilot.example.com/static/js/widget.js"
             data-api="https://copilot.example.com"
             data-widget-key="wk_..."
             data-title="Need help?"></script>

   The widget renders a floating button + chat panel, talks to the same
   /chat endpoint as the main app, and shows citations and the human
   handoff state exactly like the app does. All rendering is
   textContent-only, so answers can never inject markup into the host page.

   data-widget-key is the tenant's PUBLIC widget key (Admin → Website
   widget, or POST /widget-key). It can only ask /chat questions; it cannot
   read or change documents, users or conversations. The site's origin
   must be on the tenant's allowlist. Never put a login token (JWT) in a
   web page: the old data-token attribute is refused. */

(function (global) {
  "use strict";

  function currentScript() {
    return document.currentScript ||
      document.querySelector('script[src*="widget.js"]');
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = text; }
    return node;
  }

  function init() {
    var script = currentScript();
    if (!script) { return; }
    var apiBase = (script.getAttribute("data-api") || "").replace(/\/$/, "");
    var widgetKey = script.getAttribute("data-widget-key") || "";
    var title = script.getAttribute("data-title") || "Support Copilot";
    if (script.getAttribute("data-token") && global.console) {
      global.console.error("[copilot-widget] data-token is no longer supported: a login token in a public page gives full account access. Use data-widget-key with your public widget key instead.");
    }
    if (!widgetKey && global.console) {
      global.console.warn("[copilot-widget] data-widget-key is missing; the widget will not send messages.");
    }

    var open = false;
    var sending = false;
    var conversationId = null;

    var button = el("button", "copilot-widget-button", "💬");
    button.type = "button";
    button.setAttribute("aria-label", "Open support chat");
    button.setAttribute("aria-expanded", "false");

    var panel = el("div", "copilot-widget-panel");
    panel.hidden = true;
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", title);

    var head = el("div", "copilot-widget-head");
    head.appendChild(el("strong", null, title));
    var closeBtn = el("button", null, "✕");
    closeBtn.type = "button";
    closeBtn.setAttribute("aria-label", "Close support chat");
    head.appendChild(closeBtn);
    panel.appendChild(head);

    var log = el("div", "copilot-widget-log");
    log.setAttribute("role", "log");
    log.setAttribute("aria-live", "polite");
    log.appendChild(el("p", "empty-state", "Hi! Ask me anything about our products and policies."));
    panel.appendChild(log);

    var form = el("form", "copilot-widget-form");
    var input = el("input");
    input.type = "text";
    input.placeholder = "Type your question…";
    input.maxLength = 2000;
    input.setAttribute("aria-label", "Your question");
    var send = el("button", "primary", "Send");
    send.type = "submit";
    form.appendChild(input);
    form.appendChild(send);
    panel.appendChild(form);

    function setOpen(value) {
      open = value;
      panel.hidden = !open;
      button.setAttribute("aria-expanded", String(open));
      if (open) { input.focus(); }
    }

    button.addEventListener("click", function () { setOpen(!open); });
    closeBtn.addEventListener("click", function () { setOpen(false); });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && open) { setOpen(false); button.focus(); }
    });

    function appendMessage(role, content, citations, needsHuman) {
      var empty = log.querySelector(".empty-state");
      if (empty) { empty.remove(); }
      var wrap = el("div", "message " + role);
      wrap.appendChild(el("div", null, content));
      if (citations && citations.length) {
        var list = el("ol", "citations");
        citations.forEach(function (cite, i) {
          var item = el("li");
          item.appendChild(el("span", null, "[" + (i + 1) + "] " + cite.document_title));
          item.appendChild(el("span", "score", " — score " + Number(cite.score).toFixed(2)));
          list.appendChild(item);
        });
        wrap.appendChild(list);
      }
      if (needsHuman) {
        var banner = el("div", "handoff-banner", "I don't have a confident answer — a human agent will follow up.");
        banner.setAttribute("role", "alert");
        wrap.appendChild(banner);
      }
      log.appendChild(wrap);
      log.scrollTop = log.scrollHeight;
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var question = input.value.trim();
      if (!question || sending) { return; }
      if (!widgetKey) {
        appendMessage("assistant", "This chat widget isn't configured yet — please contact support directly.");
        return;
      }
      sending = true;
      send.disabled = true;
      appendMessage("user", question);
      input.value = "";

      var body = { question: question };
      if (conversationId) { body.conversation_id = conversationId; }
      global.fetch(apiBase + "/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Widget-Key": widgetKey },
        body: JSON.stringify(body),
      }).then(function (resp) {
        if (resp.status === 429) {
          var wait = resp.headers.get("Retry-After");
          throw new Error("You're sending messages too quickly — please wait " +
            (wait ? wait + " seconds" : "a moment") + ".");
        }
        return resp.json().catch(function () { return {}; }).then(function (data) {
          if (!resp.ok) {
            var detail = data && data.detail;
            throw new Error(typeof detail === "string" ? detail : "Request failed (" + resp.status + ")");
          }
          return data;
        });
      }).then(function (data) {
        conversationId = data.conversation_id;
        appendMessage("assistant", data.answer, data.citations, data.needs_human);
      }).catch(function (err) {
        appendMessage("assistant", "Sorry — I couldn't reach support right now. " + err.message);
      }).finally(function () {
        sending = false;
        send.disabled = false;
        input.focus();
      });
    });

    document.body.appendChild(button);
    document.body.appendChild(panel);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})(typeof window !== "undefined" ? window : globalThis);
