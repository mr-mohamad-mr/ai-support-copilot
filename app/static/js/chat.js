/* AI Support Copilot — chat view (presentation layer).
   Owns the conversation list + message log + composer. All dynamic
   content is rendered with textContent / createElement — never innerHTML —
   so document text and answers can never inject markup. */

(function (global) {
  "use strict";

  var Copilot = global.Copilot;
  var api = Copilot.api;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = text; }
    return node;
  }

  function formatTime(iso) {
    try { return new Date(iso).toLocaleString(); }
    catch (e) { return iso || ""; }
  }

  var ChatView = {
    conversationId: null,
    sending: false,

    mount: function () {
      this.log = document.getElementById("chat-log");
      this.listEl = document.getElementById("conv-list");
      this.form = document.getElementById("chat-form");
      this.input = document.getElementById("chat-input");
      this.sendBtn = document.getElementById("chat-send");
      this.newBtn = document.getElementById("chat-new");
      this.empty = document.getElementById("chat-empty");
      this.typing = document.getElementById("chat-typing");
      this.titleEl = document.getElementById("chat-title");

      this.form.addEventListener("submit", this.onSend.bind(this));
      this.newBtn.addEventListener("click", this.newConversation.bind(this));
      this.refreshList();
    },

    show: function () {
      if (!this.conversationId) { this.showEmpty(); }
      this.input.focus();
    },

    showEmpty: function () {
      this.log.replaceChildren();
      this.log.appendChild(this.empty);
      this.titleEl.textContent = "Chat";
      this.conversationId = null;
      this.markCurrent();
    },

    newConversation: function () {
      this.showEmpty();
      this.input.focus();
    },

    markCurrent: function () {
      var buttons = this.listEl.querySelectorAll("button[data-conv]");
      buttons.forEach(function (btn) {
        if (ChatView.conversationId && Number(btn.dataset.conv) === ChatView.conversationId) {
          btn.setAttribute("aria-current", "true");
        } else {
          btn.removeAttribute("aria-current");
        }
      });
    },

    refreshList: function () {
      var self = this;
      return api.listConversations(1, 20).then(function (page) {
        self.listEl.replaceChildren();
        page.items.forEach(function (conv) {
          var li = el("li");
          var btn = el("button");
          btn.type = "button";
          btn.dataset.conv = String(conv.id);
          var title = el("span", null, conv.title);
          var count = el("span", "count", conv.message_count + " msgs");
          btn.appendChild(title);
          btn.appendChild(count);
          btn.addEventListener("click", function () { self.openConversation(conv.id); });
          li.appendChild(btn);
          self.listEl.appendChild(li);
        });
        self.markCurrent();
      }).catch(function (err) {
        Copilot.app.toast("Could not load conversations: " + err.message, "error");
      });
    },

    openConversation: function (id) {
      var self = this;
      return api.getConversation(id).then(function (conv) {
        self.conversationId = conv.id;
        self.titleEl.textContent = conv.title;
        self.log.replaceChildren();
        conv.messages.forEach(function (msg) {
          self.appendMessage(msg.role, msg.content, null, msg.needs_human);
        });
        self.markCurrent();
        self.scrollToBottom();
      }).catch(function (err) {
        Copilot.app.toast("Could not open conversation: " + err.message, "error");
      });
    },

    appendMessage: function (role, content, citations, needsHuman) {
      if (this.empty.parentNode === this.log) { this.empty.remove(); }
      var wrap = el("div", "message " + role);
      var body = el("div", null, content);
      wrap.appendChild(body);

      if (citations && citations.length) {
        var list = el("ol", "citations");
        citations.forEach(function (cite, i) {
          var item = el("li");
          var label = el("span", null, "[" + (i + 1) + "] " + cite.document_title);
          var score = el("span", "score", " — score " + Number(cite.score).toFixed(2));
          item.appendChild(label);
          item.appendChild(score);
          if (cite.heading) {
            item.appendChild(el("span", null, " · " + cite.heading));
          }
          var excerpt = el("div", null, "“" + cite.excerpt + "”");
          item.appendChild(excerpt);
          list.appendChild(item);
        });
        wrap.appendChild(list);
      }

      var meta = el("div", "meta", role === "user" ? "You" : "Support Copilot");
      wrap.appendChild(meta);

      if (needsHuman) {
        var banner = el("div", "handoff-banner", "I don't have a confident answer from the knowledge base — a human agent should pick this up.");
        banner.setAttribute("role", "alert");
        wrap.appendChild(banner);
      }
      this.log.appendChild(wrap);
      return wrap;
    },

    scrollToBottom: function () {
      this.log.scrollTop = this.log.scrollHeight;
    },

    setSending: function (busy) {
      this.sending = busy;
      this.sendBtn.disabled = busy;
      this.input.disabled = busy;
      this.typing.hidden = !busy;
    },

    onSend: function (event) {
      event.preventDefault();
      var question = this.input.value.trim();
      if (!question || this.sending) { return; }
      var self = this;
      this.setSending(true);
      this.appendMessage("user", question);
      this.input.value = "";
      this.scrollToBottom();

      api.chat(question, this.conversationId).then(function (resp) {
        self.conversationId = resp.conversation_id;
        self.appendMessage("assistant", resp.answer, resp.citations, resp.needs_human);
        self.scrollToBottom();
        self.refreshList();
      }).catch(function (err) {
        Copilot.app.toast("Chat failed: " + err.message, "error");
        if (err.status === 401) { Copilot.app.requireLogin(); }
      }).finally(function () {
        self.setSending(false);
        self.input.focus();
      });
    },
  };

  Copilot.chat = ChatView;
})(typeof window !== "undefined" ? window : globalThis);
