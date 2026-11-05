// Simple utility functions for the dashboard
document.addEventListener('DOMContentLoaded', function() {
    // Auto-hide flash messages after 5 seconds
    const flashMessages = document.querySelectorAll('.flash-message');
    flashMessages.forEach(function(msg) {
        setTimeout(function() {
            msg.style.opacity = '0';
            msg.style.transition = 'opacity 0.5s';
            setTimeout(function() {
                msg.remove();
            }, 500);
        }, 5000);
    });

    // Status indicator in sidebar
    fetchHealthIndicator();

    // Dashboard nested tabs (vertical -> state -> category).
    // Each [data-tab-group] is independent; panes are matched by
    // [data-tab-pane-group="<group name>"] so nested groups don't clash.
    // Selection is persisted per-group across refresh/navigation, unless the
    // group is marked data-tab-persist="off" — then the pane the server
    // rendered stays visible (the server knows which tab the active filters
    // actually matched, and a stale stored choice would hide it).
    // data-tab-url="on" mirrors the choice into ?cat= so a refresh keeps it.
    const TAB_STORAGE_PREFIX = 'investclosure_tab_';

    function setupTabGroup(group) {
        const groupName = group.getAttribute('data-tab-group');
        const persist = group.getAttribute('data-tab-persist') !== 'off';
        const syncUrl = group.getAttribute('data-tab-url') === 'on';
        // Direct-child buttons only (avoids matching buttons of nested groups)
        const buttons = Array.from(group.querySelectorAll(':scope > [data-tab]'));
        const panes = Array.from(
            document.querySelectorAll('[data-tab-pane-group="' + groupName + '"]')
        );

        function activateTab(target) {
            buttons.forEach(function(b) {
                const active = b.getAttribute('data-tab') === target;
                b.classList.toggle('active', active);
                b.setAttribute('aria-selected', active ? 'true' : 'false');
            });
            panes.forEach(function(p) {
                p.hidden = p.getAttribute('data-tab-pane') !== target;
            });
        }

        function syncCatParam(target) {
            if (!window.history || !window.history.replaceState) return;
            try {
                const url = new URL(window.location.href);
                url.searchParams.set('cat', target);
                window.history.replaceState({}, '', url);
            } catch (e) { /* non-standard URL: nothing to sync */ }
        }

        buttons.forEach(function(btn) {
            btn.addEventListener('click', function() {
                const target = btn.getAttribute('data-tab');
                activateTab(target);
                if (persist) {
                    try { localStorage.setItem(TAB_STORAGE_PREFIX + groupName, target); } catch (e) {}
                }
                if (syncUrl) syncCatParam(target);
            });
        });

        // Restore the stored choice when the group allows it; otherwise honour
        // the pane the server rendered (its .active button).
        let saved = null;
        if (persist) {
            try { saved = localStorage.getItem(TAB_STORAGE_PREFIX + groupName); } catch (e) {}
        }
        const initial = saved && buttons.some(function(b) { return b.getAttribute('data-tab') === saved; })
            ? saved
            : (persist
                ? (buttons.length ? buttons[0].getAttribute('data-tab') : null)
                : (buttons.some(function(b) { return b.classList.contains('active'); })
                    ? null : (buttons.length ? buttons[0].getAttribute('data-tab') : null)));
        if (initial) activateTab(initial);
    }

    document.querySelectorAll('[data-tab-group]').forEach(setupTabGroup);
});

async function fetchHealthIndicator() {
    try {
        const resp = await fetch('/health');
        const data = await resp.json();
        
        const statusIndicator = document.getElementById('health-status');
        if (statusIndicator) {
            statusIndicator.textContent = data.status;
            statusIndicator.style.color = 
                data.status === 'healthy' ? '#22c55e' : 
                data.status === 'degraded' ? '#f59e0b' : '#ef4444';
        }
    } catch (e) {
        console.error('Failed to fetch health:', e);
    }
}

// Refresh health every 60 seconds
setInterval(fetchHealthIndicator, 60000);

// Property notes editing lives on the property detail page (see property.html).


// Bulk archive selection: live count, select-all (with tri-state), and a
// confirm guard on the bulk submit. Scoped to each [data-bulk-form] so
// dashboard state/category tabs never cross-select.
document.querySelectorAll('[data-bulk-form]').forEach(function(form) {
    var checks = Array.from(form.querySelectorAll('input[name="ids"]'));
    var selAll = form.querySelector('[data-bulk-select-all]');
    var countEl = form.querySelector('[data-bulk-count]');
    var submitBtn = form.querySelector('[data-bulk-submit]');

    function update() {
        var checked = form.querySelectorAll('input[name="ids"]:checked').length;
        if (countEl) countEl.textContent = String(checked);
        if (submitBtn) {
            submitBtn.disabled = checked === 0;
        }
        if (selAll) {
            selAll.checked = checks.length > 0 && checked === checks.length;
            selAll.indeterminate = checked > 0 && checked < checks.length;
        }
    }

    if (selAll && checks.length) {
        selAll.addEventListener('click', function() {
            // resolve indeterminate: clicking it checks everything
            selAll.checked = selAll.indeterminate ? true : selAll.checked;
        });
        selAll.addEventListener('change', function() {
            checks.forEach(function(c) { c.checked = selAll.checked; });
            update();
        });
    }
    checks.forEach(function(c) { c.addEventListener('change', update); });

    if (submitBtn) {
        submitBtn.addEventListener('click', function(e) {
            var checked = form.querySelectorAll('input[name="ids"]:checked');
            if (!checked.length) {
                e.preventDefault();
                return;
            }
            if (!confirm('Archive ' + checked.length + ' selected propert'
                         + (checked.length === 1 ? 'y' : 'ies') + '?')) {
                e.preventDefault();
            }
        });
    }
    update();
});
