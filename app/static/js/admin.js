/* AI Support Copilot — admin view (presentation layer).
   Documents (upload / list / delete), the website widget key and its
   allowed origins, and conversations (list / inspect / delete). Shows what
   an operator actually needs — what's in the knowledge base, and which
   chats need a human. All rendering is textContent-only. */

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

  var AdminView = {
    mount: function () {
      this.docTable = document.getElementById("doc-table-body");
      this.docEmpty = document.getElementById("doc-empty");
      this.docForm = document.getElementById("doc-form");
      this.docTitle = document.getElementById("doc-title");
      this.docSource = document.getElementById("doc-source");
      this.docContent = document.getElementById("doc-content");
      this.docCount = document.getElementById("doc-count");
      this.convList = document.getElementById("admin-conv-list");
      this.convDetail = document.getElementById("admin-conv-detail");
      this.convDetailTitle = document.getElementById("admin-conv-title");

      this.widgetStatus = document.getElementById("widget-key-status");
      this.widgetNew = document.getElementById("widget-key-new");
      this.widgetValue = document.getElementById("widget-key-value");
      this.widgetSnippet = document.getElementById("widget-snippet");
      this.widgetRotate = document.getElementById("widget-key-rotate");
      this.widgetForm = document.getElementById("widget-key-form");
      this.widgetOrigins = document.getElementById("widget-origins");

      this.docForm.addEventListener("submit", this.onUpload.bind(this));
      this.widgetRotate.addEventListener("click", this.onRotateWidgetKey.bind(this));
      this.widgetForm.addEventListener("submit", this.onSaveOrigins.bind(this));
      this.docContent.addEventListener("input", this.updateCharCount.bind(this));
      this.updateCharCount();
    },

    show: function () {
      this.refreshDocuments();
      this.refreshWidgetKey();
      this.refreshConversations();
    },

    updateCharCount: function () {
      this.docCount.textContent = this.docContent.value.length.toLocaleString() + " characters";
    },

    /* --- Website widget -------------------------------------------- */

    renderWidgetInfo: function (info) {
      this.widgetStatus.textContent = info.configured
        ? "Active key " + info.key_prefix + " (created " + formatTime(info.created_at) + ")."
        : "No widget key yet.";
      this.widgetRotate.textContent = info.configured ? "Rotate widget key" : "Create widget key";
      this.widgetOrigins.value = (info.allowed_origins || []).join("\n");
    },

    refreshWidgetKey: function () {
      var self = this;
      return api.getWidgetKey().then(function (info) {
        self.renderWidgetInfo(info);
      }).catch(function (err) {
        // Agents may not manage the widget: hide the card for them.
        if (err.status === 403) {
          document.getElementById("widget-card").hidden = true;
          return;
        }
        Copilot.app.toast("Could not load the widget key: " + err.message, "error");
      });
    },

    onRotateWidgetKey: function () {
      var self = this;
      var rotating = this.widgetRotate.textContent.indexOf("Rotate") === 0;
      if (rotating && !global.confirm("Rotate the widget key? The current key stops working immediately.")) {
        return;
      }
      api.rotateWidgetKey().then(function (created) {
        self.renderWidgetInfo(created);
        self.widgetNew.hidden = false;
        self.widgetValue.value = created.key;
        var origin = global.location ? global.location.origin : "";
        self.widgetSnippet.textContent =
          "<script src=\"" + origin + "/static/js/widget.js\"\n" +
          "        data-api=\"" + origin + "\"\n" +
          "        data-widget-key=\"" + created.key + "\"\n" +
          "        data-title=\"Need help?\"></script>";
        Copilot.app.toast("Widget key ready — copy it now, it is shown only once.", "ok");
      }).catch(function (err) {
        Copilot.app.toast("Could not create the widget key: " + err.message, "error");
      });
    },

    onSaveOrigins: function (event) {
      event.preventDefault();
      var self = this;
      var origins = this.widgetOrigins.value.split(/\s+/).filter(Boolean);
      api.setWidgetOrigins(origins).then(function (info) {
        self.renderWidgetInfo(info);
        Copilot.app.toast("Allowed origins saved.", "ok");
      }).catch(function (err) {
        Copilot.app.toast("Could not save origins: " + err.message, "error");
      });
    },

    /* --- Documents -------------------------------------------------- */

    refreshDocuments: function () {
      var self = this;
      return api.listDocuments(1, 100).then(function (page) {
        self.docTable.replaceChildren();
        self.docEmpty.hidden = page.items.length > 0;
        page.items.forEach(function (doc) {
          var tr = el("tr");
          tr.appendChild(el("td", null, doc.title));
          tr.appendChild(el("td", null, doc.source));
          tr.appendChild(el("td", "num", String(doc.char_count)));
          tr.appendChild(el("td", "num", String(doc.chunk_count)));
          tr.appendChild(el("td", null, formatTime(doc.created_at)));
          var actions = el("td", "doc-actions");
          var del = el("button", "danger", "Delete");
          del.type = "button";
          del.setAttribute("aria-label", "Delete document " + doc.title);
          del.addEventListener("click", function () { self.deleteDocument(doc); });
          actions.appendChild(del);
          tr.appendChild(actions);
          self.docTable.appendChild(tr);
        });
      }).catch(function (err) {
        Copilot.app.toast("Could not load documents: " + err.message, "error");
      });
    },

    onUpload: function (event) {
      event.preventDefault();
      var title = this.docTitle.value.trim();
      var content = this.docContent.value.trim();
      var source = this.docSource.value.trim() || "manual";
      if (!title || !content) {
        Copilot.app.toast("Title and content are both required.", "error");
        return;
      }
      var self = this;
      api.createDocument(title, content, source).then(function (doc) {
        Copilot.app.toast(
          "Document added: " + doc.title + " (" + doc.chunk_count + " chunk" +
          (doc.chunk_count === 1 ? "" : "s") + ").", "ok");
        self.docForm.reset();
        self.updateCharCount();
        self.refreshDocuments();
      }).catch(function (err) {
        Copilot.app.toast("Upload failed: " + err.message, "error");
      });
    },

    deleteDocument: function (doc) {
      var self = this;
      if (!global.confirm("Delete document “" + doc.title + "” and its " +
          doc.chunk_count + " chunks? Chats can no longer cite it.")) {
        return;
      }
      api.deleteDocument(doc.id).then(function () {
        Copilot.app.toast("Document deleted.", "ok");
        self.refreshDocuments();
      }).catch(function (err) {
        Copilot.app.toast("Delete failed: " + err.message, "error");
      });
    },

    /* --- Conversations ---------------------------------------------- */

    refreshConversations: function () {
      var self = this;
      return api.listConversations(1, 20).then(function (page) {
        self.convList.replaceChildren();
        if (!page.items.length) {
          self.convList.appendChild(el("li", null, "No conversations yet — ask something in Chat."));
          return;
        }
        page.items.forEach(function (conv) {
          var li = el("li");
          var btn = el("button");
          btn.type = "button";
          btn.dataset.conv = String(conv.id);
          btn.appendChild(el("span", null, conv.title));
          btn.appendChild(el("span", "count", conv.message_count + " msgs · " + formatTime(conv.created_at)));
          btn.addEventListener("click", function () { self.openConversation(conv.id); });
          li.appendChild(btn);
          self.convList.appendChild(li);
        });
      }).catch(function (err) {
        Copilot.app.toast("Could not load conversations: " + err.message, "error");
      });
    },

    openConversation: function (id) {
      var self = this;
      api.getConversation(id).then(function (conv) {
        self.convDetail.hidden = false;
        self.convDetailTitle.textContent = conv.title;
        var needsHuman = conv.messages.some(function (m) { return m.needs_human; });
        var badge = el("span", "badge " + (needsHuman ? "handoff" : "ok"),
          needsHuman ? "Needs human" : "Answered");
        self.convDetailTitle.appendChild(document.createTextNode(" "));
        self.convDetailTitle.appendChild(badge);

        var body = document.getElementById("admin-conv-messages");
        body.replaceChildren();
        conv.messages.forEach(function (msg) {
          var wrap = el("div", "message " + msg.role);
          wrap.appendChild(el("div", null, msg.content));
          var meta = el("div", "meta",
            (msg.role === "user" ? "Customer" : "Support Copilot") +
            " · " + formatTime(msg.created_at) +
            (msg.needs_human ? " · flagged for human handoff" : ""));
          wrap.appendChild(meta);
          body.appendChild(wrap);
        });

        var del = document.getElementById("admin-conv-delete");
        del.onclick = function () {
          if (!global.confirm("Delete this conversation?")) { return; }
          api.deleteConversation(conv.id).then(function () {
            Copilot.app.toast("Conversation deleted.", "ok");
            self.convDetail.hidden = true;
            self.refreshConversations();
          }).catch(function (err) {
            Copilot.app.toast("Delete failed: " + err.message, "error");
          });
        };
      }).catch(function (err) {
        Copilot.app.toast("Could not open conversation: " + err.message, "error");
      });
    },
  };

  Copilot.admin = AdminView;
})(typeof window !== "undefined" ? window : globalThis);
