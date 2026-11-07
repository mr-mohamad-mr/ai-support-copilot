/* AI Support Copilot — analytics view (presentation layer, stage 4).
   Headline numbers, a daily-questions bar chart, the documents that
   actually answer questions, and the questions that still needed a
   human. Guided by dashboard_builder: what an operator needs to act
   on, not every metric available. All rendering is textContent-only;
   the bar chart is plain divs, so no chart library is needed. */

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

  function pct(rate) {
    return (rate * 100).toFixed(1) + "%";
  }

  var AnalyticsView = {
    days: 14,

    mount: function () {
      this.cards = document.getElementById("analytics-cards");
      this.chart = document.getElementById("analytics-chart");
      this.chartEmpty = document.getElementById("analytics-chart-empty");
      this.topDocs = document.getElementById("analytics-top-docs");
      this.topDocsEmpty = document.getElementById("analytics-top-docs-empty");
      this.handoffs = document.getElementById("analytics-handoffs");
      this.handoffsEmpty = document.getElementById("analytics-handoffs-empty");
      this.daysLabel = document.getElementById("analytics-days-label");
      var self = this;
      document.querySelectorAll("#analytics-range button").forEach(function (btn) {
        btn.addEventListener("click", function () {
          self.days = parseInt(btn.dataset.days, 10);
          self.show();
        });
      });
    },

    show: function () {
      var self = this;
      document.querySelectorAll("#analytics-range button").forEach(function (btn) {
        btn.setAttribute("aria-pressed", String(parseInt(btn.dataset.days, 10) === self.days));
      });
      this.daysLabel.textContent = "Last " + this.days + " days";
      return api.getAnalytics(this.days).then(function (summary) {
        self.render(summary);
      }).catch(function (err) {
        Copilot.app.toast("Could not load analytics: " + err.message, "error");
      });
    },

    render: function (summary) {
      this.renderCards(summary);
      this.renderChart(summary.daily);
      this.renderTopDocs(summary.top_documents);
      this.renderHandoffs(summary.recent_handoffs);
    },

    renderCards: function (summary) {
      var cards = [
        { label: "Questions asked", value: String(summary.total_questions) },
        { label: "Conversations", value: String(summary.total_conversations) },
        { label: "Answered by Copilot", value: String(summary.answered) },
        { label: "Handed off to a human", value: String(summary.handed_off) },
        { label: "Handoff rate", value: pct(summary.handoff_rate) },
        {
          label: "Avg. citation score",
          value: summary.avg_citation_score === null
            ? "—"
            : summary.avg_citation_score.toFixed(2),
        },
      ];
      this.cards.replaceChildren();
      cards.forEach(function (card) {
        var wrap = el("div", "metric-card");
        wrap.appendChild(el("div", "metric-value", card.value));
        wrap.appendChild(el("div", "metric-label", card.label));
        this.cards.appendChild(wrap);
      }, this);
    },

    renderChart: function (daily) {
      var max = 0;
      daily.forEach(function (day) { if (day.questions > max) { max = day.questions; } });
      this.chart.replaceChildren();
      this.chartEmpty.hidden = max > 0;
      if (!max) { return; }
      var self = this;
      daily.forEach(function (day) {
        var col = el("div", "bar-col");
        var bar = el("div", "bar");
        bar.style.height = Math.max(day.questions / max * 100, day.questions ? 4 : 0) + "%";
        bar.title = day.date + ": " + day.questions + " questions (" +
          day.answered + " answered, " + day.handed_off + " handed off)";
        bar.setAttribute("role", "img");
        bar.setAttribute("aria-label", bar.title);
        col.appendChild(bar);
        col.appendChild(el("div", "bar-value", String(day.questions)));
        col.appendChild(el("div", "bar-label", day.date.slice(5)));
        self.chart.appendChild(col);
      });
    },

    renderTopDocs: function (topDocs) {
      this.topDocs.replaceChildren();
      this.topDocsEmpty.hidden = topDocs.length > 0;
      topDocs.forEach(function (doc) {
        var tr = el("tr");
        tr.appendChild(el("td", null, doc.document_title));
        tr.appendChild(el("td", "num", String(doc.citations)));
        this.topDocs.appendChild(tr);
      }, this);
    },

    renderHandoffs: function (handoffs) {
      this.handoffs.replaceChildren();
      this.handoffsEmpty.hidden = handoffs.length > 0;
      handoffs.forEach(function (item) {
        var li = el("li");
        li.appendChild(el("span", null, item.question));
        try {
          li.appendChild(el("span", "count", new Date(item.created_at).toLocaleString()));
        } catch (e) {
          li.appendChild(el("span", "count", item.created_at));
        }
        this.handoffs.appendChild(li);
      }, this);
    },
  };

  Copilot.analytics = AnalyticsView;
})(typeof window !== "undefined" ? window : globalThis);
