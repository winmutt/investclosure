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
