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

    // Dashboard tabs (Foreclosure Listings / Notices) — sticky across refresh/navigation
    const TAB_STORAGE_KEY = 'investclosure_active_tab';
    const tabGroups = document.querySelectorAll('[data-tab-group]');
    tabGroups.forEach(function(group) {
        const buttons = Array.from(group.querySelectorAll('[data-tab]'));
        const panes = Array.from(document.querySelectorAll('[data-tab-pane]'));

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
                try { localStorage.setItem(TAB_STORAGE_KEY, target); } catch (e) {}
            });
        });

        // Restore the previously selected tab after refresh or navigation
        let saved = null;
        try { saved = localStorage.getItem(TAB_STORAGE_KEY); } catch (e) {}
        if (saved && buttons.some(function(b) { return b.getAttribute('data-tab') === saved; })) {
            activateTab(saved);
        }
    });
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
