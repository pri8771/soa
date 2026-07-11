import { calculateCampaignReadiness } from "@marketing-ops/domain";
import { demoCampaign, demoLaunchItems } from "@marketing-ops/test-fixtures";
import { MetricCard, ProductMark, StatusPill, Surface } from "@marketing-ops/ui";

import { summarizePortfolioHealth } from "@/lib/portfolio-health";

const navigation = [
  "Home",
  "Campaigns",
  "Work",
  "Calendar",
  "Content",
  "Assets",
  "Approvals",
  "Publishing",
  "Analytics",
  "Automations",
] as const;

const readiness = calculateCampaignReadiness([
  { id: "brief", label: "Brief approved", complete: true, blocking: true },
  { id: "message", label: "Message house approved", complete: true, blocking: true },
  { id: "landing", label: "Landing page ready", complete: true, blocking: true },
  { id: "video", label: "Launch video approved", complete: false, blocking: true },
  { id: "legal", label: "Legal review complete", complete: false, blocking: true },
  { id: "social", label: "Social sequence approved", complete: false, blocking: false },
  { id: "tracking", label: "Tracking verified", complete: true, blocking: true },
  { id: "sales", label: "Sales enablement ready", complete: true, blocking: false },
  { id: "support", label: "Support briefing complete", complete: true, blocking: false },
]);

const portfolio = summarizePortfolioHealth([
  { blocked: 2, dueSoon: 3, status: "at-risk" },
  { blocked: 0, dueSoon: 1, status: "on-track" },
  { blocked: 0, dueSoon: 0, status: "on-track" },
]);

function Icon({ name }: { readonly name: "spark" | "search" | "bell" | "plus" | "arrow" }) {
  const paths = {
    arrow: "M5 12h14m-6-6 6 6-6 6",
    bell: "M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4",
    plus: "M12 5v14M5 12h14",
    search: "m21 21-4.35-4.35m2.35-5.65a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z",
    spark: "m12 3 1.5 4.5L18 9l-4.5 1.5L12 15l-1.5-4.5L6 9l4.5-1.5L12 3Z",
  } as const;

  return (
    <svg aria-hidden="true" fill="none" height="18" viewBox="0 0 24 24" width="18">
      <path
        d={paths[name]}
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.8"
      />
    </svg>
  );
}

export default function HomePage() {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar__brand">
          <ProductMark />
        </div>

        <button className="workspace-switcher" type="button">
          <span className="workspace-switcher__avatar">NS</span>
          <span>
            <strong>Northstar</strong>
            <small>Growth workspace</small>
          </span>
          <span aria-hidden="true" className="workspace-switcher__chevron">
            ⌄
          </span>
        </button>

        <nav aria-label="Primary navigation" className="primary-nav">
          {navigation.map((item, index) => (
            <a aria-current={index === 0 ? "page" : undefined} href="#" key={item}>
              <span aria-hidden="true" className="primary-nav__dot" />
              {item}
              {item === "Approvals" ? <span className="primary-nav__count">5</span> : null}
            </a>
          ))}
        </nav>

        <div className="sidebar__footer">
          <div className="connection-health">
            <span aria-hidden="true" className="connection-health__dot" />
            <span>
              <strong>Connections healthy</strong>
              <small>3 social accounts</small>
            </span>
          </div>
          <button aria-label="Open profile menu" className="profile-button" type="button">
            MC
          </button>
        </div>
      </aside>

      <main className="main-area">
        <header className="global-bar">
          <div className="context-trail">
            <span>Northstar</span>
            <span aria-hidden="true">/</span>
            <strong>Home</strong>
          </div>
          <div className="global-actions">
            <button className="search-button" type="button">
              <Icon name="search" />
              <span>Search campaigns, work, content…</span>
              <kbd>⌘ K</kbd>
            </button>
            <button aria-label="Notifications" className="icon-button" type="button">
              <Icon name="bell" />
              <span className="notification-dot" />
            </button>
            <button className="primary-button" type="button">
              <Icon name="plus" />
              Create
            </button>
          </div>
        </header>

        <div className="page-content">
          <section className="page-heading">
            <div>
              <p className="eyebrow">Friday, July 11</p>
              <h1>Good morning, Maya.</h1>
              <p className="page-heading__summary">
                Three launches need attention. Two approvals are holding scheduled content.
              </p>
            </div>
            <div className="page-heading__actions">
              <button className="secondary-button" type="button">
                View calendar
              </button>
              <button className="primary-button" type="button">
                <Icon name="spark" />
                Start campaign
              </button>
            </div>
          </section>

          <section aria-label="Portfolio health" className="metric-grid">
            <MetricCard
              change="+8%"
              detail="Across 7 active campaigns"
              label="On-time work"
              value={`${portfolio.onTrackRate}%`}
            />
            <MetricCard detail="2 client · 3 internal" label="Waiting approval" value="5" />
            <MetricCard
              detail="LinkedIn account requires reconnect"
              label="Publishing risk"
              value="1"
            />
            <MetricCard
              change="+14%"
              detail="Rolling 30-day engagement"
              label="Content momentum"
              value="4.8%"
            />
          </section>

          <section className="workspace-grid">
            <Surface className="launch-focus">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">Launch focus</p>
                  <h2>{demoCampaign.campaign}</h2>
                </div>
                <StatusPill tone="danger">Blocked</StatusPill>
              </div>

              <div className="campaign-meta">
                <span>{demoCampaign.brand}</span>
                <span>Owner · {demoCampaign.owner}</span>
                <span>Launch · Jul 24</span>
              </div>

              <div className="readiness-row">
                <div>
                  <strong>
                    {readiness.completed} of {readiness.total}
                  </strong>
                  <span>launch requirements complete</span>
                </div>
                <div
                  aria-label={`${readiness.completed} of ${readiness.total} complete`}
                  className="progress-track"
                >
                  <span style={{ width: `${(readiness.completed / readiness.total) * 100}%` }} />
                </div>
              </div>

              <div className="launch-list">
                {demoLaunchItems.map((item) => (
                  <article className="launch-item" key={item.id}>
                    <span
                      aria-hidden="true"
                      className={`launch-item__state launch-item__state--${item.status}`}
                    />
                    <div>
                      <strong>{item.label}</strong>
                      <span>{item.owner}</span>
                    </div>
                    <StatusPill
                      tone={
                        item.status === "complete"
                          ? "positive"
                          : item.status === "blocked"
                            ? "danger"
                            : item.status === "at-risk"
                              ? "warning"
                              : "accent"
                      }
                    >
                      {item.status.replace("-", " ")}
                    </StatusPill>
                  </article>
                ))}
              </div>

              <a className="text-link" href="#">
                Open Campaign Room
                <Icon name="arrow" />
              </a>
            </Surface>

            <div className="right-column">
              <Surface className="attention-panel">
                <div className="section-heading section-heading--compact">
                  <div>
                    <p className="eyebrow">Needs attention</p>
                    <h2>Next actions</h2>
                  </div>
                  <span className="section-count">4</span>
                </div>

                <div className="attention-list">
                  <article>
                    <span className="attention-icon attention-icon--danger">!</span>
                    <div>
                      <strong>Reconnect Northstar LinkedIn</strong>
                      <p>Three scheduled posts are at risk after a token refresh failed.</p>
                    </div>
                    <span>Now</span>
                  </article>
                  <article>
                    <span className="attention-icon attention-icon--warning">↗</span>
                    <div>
                      <strong>Executive approval overdue</strong>
                      <p>Signal 2.0 launch video has waited 18 hours.</p>
                    </div>
                    <span>18h</span>
                  </article>
                  <article>
                    <span className="attention-icon attention-icon--accent">⌁</span>
                    <div>
                      <strong>Webinar sequence ready</strong>
                      <p>Five channel variants passed checks and can enter review.</p>
                    </div>
                    <span>1h</span>
                  </article>
                </div>
              </Surface>

              <Surface className="schedule-panel">
                <div className="section-heading section-heading--compact">
                  <div>
                    <p className="eyebrow">Today</p>
                    <h2>Publishing schedule</h2>
                  </div>
                  <a href="#">Full calendar</a>
                </div>
                <div className="schedule-timeline">
                  <article>
                    <time>10:00</time>
                    <span className="schedule-line" />
                    <div>
                      <strong>Analytics benchmark carousel</strong>
                      <p>LinkedIn · Northstar Cloud</p>
                    </div>
                    <StatusPill tone="positive">Ready</StatusPill>
                  </article>
                  <article>
                    <time>14:30</time>
                    <span className="schedule-line" />
                    <div>
                      <strong>Signal 2.0 teaser</strong>
                      <p>Instagram · Northstar Cloud</p>
                    </div>
                    <StatusPill tone="warning">Awaiting</StatusPill>
                  </article>
                  <article>
                    <time>16:00</time>
                    <span className="schedule-line" />
                    <div>
                      <strong>Webinar speaker quote</strong>
                      <p>LinkedIn · Field Notes</p>
                    </div>
                    <StatusPill tone="accent">Scheduled</StatusPill>
                  </article>
                </div>
              </Surface>
            </div>
          </section>

          <section className="campaign-table-section">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Portfolio</p>
                <h2>Active campaigns</h2>
              </div>
              <div className="table-actions">
                <button className="secondary-button" type="button">
                  Filter
                </button>
                <button className="secondary-button" type="button">
                  Customize view
                </button>
              </div>
            </div>
            <div className="campaign-table" role="table" aria-label="Active campaigns">
              <div className="campaign-table__row campaign-table__header" role="row">
                <span role="columnheader">Campaign</span>
                <span role="columnheader">Owner</span>
                <span role="columnheader">Readiness</span>
                <span role="columnheader">Next milestone</span>
                <span role="columnheader">Status</span>
              </div>
              <div className="campaign-table__row" role="row">
                <div role="cell">
                  <strong>Signal 2.0 product launch</strong>
                  <span>Northstar Cloud</span>
                </div>
                <span role="cell">Maya Chen</span>
                <span role="cell">6 / 9 complete</span>
                <span role="cell">Video approval · Jul 14</span>
                <span role="cell">
                  <StatusPill tone="danger">Blocked</StatusPill>
                </span>
              </div>
              <div className="campaign-table__row" role="row">
                <div role="cell">
                  <strong>State of RevOps webinar</strong>
                  <span>Northstar Cloud</span>
                </div>
                <span role="cell">Jordan Lee</span>
                <span role="cell">8 / 10 complete</span>
                <span role="cell">Social review · Today</span>
                <span role="cell">
                  <StatusPill tone="warning">At risk</StatusPill>
                </span>
              </div>
              <div className="campaign-table__row" role="row">
                <div role="cell">
                  <strong>Operator stories</strong>
                  <span>Field Notes</span>
                </div>
                <span role="cell">Eli Brooks</span>
                <span role="cell">5 / 5 complete</span>
                <span role="cell">Publish · Jul 15</span>
                <span role="cell">
                  <StatusPill tone="positive">On track</StatusPill>
                </span>
              </div>
            </div>
          </section>
        </div>
      </main>
    </div>
  );
}
