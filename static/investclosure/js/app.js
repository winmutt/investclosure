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

    // Dashboard nested tabs (state -> foreclosures/notices).
    // Each [data-tab-group] is independent; panes are matched by
    // [data-tab-pane-group="<group name>"] so nested groups don't clash.
    // Selection is persisted per-group across refresh/navigation.
    const TAB_STORAGE_PREFIX = 'investclosure_tab_';

    function setupTabGroup(group) {
        const groupName = group.getAttribute('data-tab-group');
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

        buttons.forEach(function(btn) {
            btn.addEventListener('click', function() {
                const target = btn.getAttribute('data-tab');
                activateTab(target);
                try { localStorage.setItem(TAB_STORAGE_PREFIX + groupName, target); } catch (e) {}
            });
        });

        // Restore previously selected tab, else default to first button
        let saved = null;
        try { saved = localStorage.getItem(TAB_STORAGE_PREFIX + groupName); } catch (e) {}
        if (saved && buttons.some(function(b) { return b.getAttribute('data-tab') === saved; })) {
            activateTab(saved);
        } else if (buttons.length) {
            activateTab(buttons[0].getAttribute('data-tab'));
        }
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

