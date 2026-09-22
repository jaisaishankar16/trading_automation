/**
 * Financial Intelligence & Trading Automation Dashboard
 * Frontend Orchestration Script
 */

(function () {
    'use strict';

    // Application State
    const state = {
        records: [],
        filteredRecords: [],
        isPolling: false,
        pollIntervalId: null,
        pollFailures: 0,
        currentModalRecord: null,
    };

    // DOM Elements
    const elements = {
        // Layout Containers
        mainContent: document.getElementById('main-content'),

        // Status & Metas
        statusDot: document.querySelector('.status-dot'),
        statusText: document.getElementById('status-text'),
        symbolCountDisplay: document.getElementById('symbol-count-display'),
        symbolFilterStat: document.getElementById('symbol-filter-stat'),
        rocCountDisplay: document.getElementById('roc-count-display') || document.getElementById('roce-count-display'),
        completedBadge: document.getElementById('completed-badge'),
        relativeTimeDisplay: document.getElementById('relative-time-display'),
        filterBadge: document.getElementById('filter-badge'),
        tableRowCount: document.getElementById('table-row-count'),

        // Action Buttons & Controls
        refreshBtn: document.getElementById('refresh-btn'),
        refreshBtnText: document.getElementById('refresh-btn-text'),
        refreshIcon: document.querySelector('.refresh-icon'),
        downloadReportBtn: document.getElementById('download-report-btn'),
        searchInput: document.getElementById('search-input'),
        clearSearchBtn: document.getElementById('clear-search-btn'),

        // Full-Page Progress Overlay
        progressOverlay: document.getElementById('progress-overlay'),
        progressBarFill: document.getElementById('progress-bar-fill'),
        progressPercent: document.getElementById('progress-percent'),
        progressStep: document.getElementById('progress-step'),

        // Table & State Views
        analysisTable: document.getElementById('analysis-table'),
        analysisTableBody: document.getElementById('analysis-table-body'),
        emptyState: document.getElementById('empty-state'),
        noResultsState: document.getElementById('no-results-state'),

        // Alert Banner & Toast
        errorBanner: document.getElementById('error-banner'),
        errorTitle: document.getElementById('error-title'),
        errorMessage: document.getElementById('error-message'),
        closeErrorBtn: document.getElementById('close-error-btn'),
        toast: document.getElementById('toast'),

        // Modal Elements
        confirmRefreshModal: document.getElementById('confirm-refresh-modal'),
        confirmRefreshCancel: document.getElementById('confirm-refresh-cancel'),
        confirmRefreshOk: document.getElementById('confirm-refresh-ok'),

        expenseModal: document.getElementById('expense-modal'),
        modalSymbolBadge: document.getElementById('modal-symbol-badge'),
        modalCompanyName: document.getElementById('modal-company-name'),
        modalRevenueVal: document.getElementById('modal-revenue-val'),
        modalCapEmployedVal: document.getElementById('modal-cap-employed-val'),
        modalSelectedTotalVal: document.getElementById('modal-selected-total-val'),
        modalExpensesList: document.getElementById('modal-expenses-list'),
        modalPreviewOpProfit: document.getElementById('modal-preview-op-profit'),
        modalPreviewRoc: document.getElementById('modal-preview-roc') || document.getElementById('modal-preview-roce'),
        modalSelectAllBtn: document.getElementById('modal-select-all-btn'),
        modalClearAllBtn: document.getElementById('modal-clear-all-btn'),
        modalCancelBtn: document.getElementById('modal-cancel-btn'),
        modalCloseBtn: document.getElementById('modal-close-btn'),
        modalCalculateBtn: document.getElementById('modal-calculate-btn'),
    };

    // -----------------------------------------------------------------------
    // Formatting Utilities
    // -----------------------------------------------------------------------
    function formatNumber(val, decimals = 2) {
        if (val === null || val === undefined || isNaN(val)) return '—';
        return Number(val).toLocaleString('en-IN', {
            minimumFractionDigits: decimals,
            maximumFractionDigits: decimals,
        });
    }

    function showToast(message, durationMs = 3500) {
        if (!elements.toast) return;
        elements.toast.textContent = message;
        elements.toast.classList.remove('hidden');
        elements.toast.style.opacity = '1';
        elements.toast.style.transform = 'translateY(0)';

        setTimeout(() => {
            elements.toast.style.opacity = '0';
            elements.toast.style.transform = 'translateY(8px)';
            setTimeout(() => elements.toast.classList.add('hidden'), 300);
        }, durationMs);
    }

    function showError(title, message) {
        if (!elements.errorBanner) return;
        elements.errorTitle.textContent = title;
        elements.errorMessage.textContent = message;
        elements.errorBanner.classList.remove('hidden');
    }

    function hideError() {
        if (!elements.errorBanner) return;
        elements.errorBanner.classList.add('hidden');
    }

    // -----------------------------------------------------------------------
    // Data Fetching & Table Rendering
    // -----------------------------------------------------------------------
    async function fetchAnalysisData() {
        try {
            const resp = await fetch('/api/analysis', {
                headers: { 'Cache-Control': 'no-cache' }
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            state.records = Array.isArray(data) ? data : [];
            filterAndRender();
        } catch (err) {
            console.error('Failed to load analysis data:', err);
        }
    }

    function filterAndRender() {
        const query = (elements.searchInput.value || '').trim().toLowerCase();
        if (!query) {
            state.filteredRecords = [...state.records];
        } else {
            state.filteredRecords = state.records.filter(r => {
                const sym = (r.symbol || '').toLowerCase();
                const name = (r.company_name || '').toLowerCase();
                return sym.includes(query) || name.includes(query);
            });
        }
        renderTable();
        updateStats();
    }

    function updateStats() {
        const total = state.records.length;
        const completed = state.records.filter(r => r.status === 'complete' && (r.roc !== null && r.roc !== undefined || r.roce !== null && r.roce !== undefined)).length;

        elements.symbolCountDisplay.textContent = total ? total.toString() : '--';
        elements.symbolFilterStat.textContent = total ? `${total} stock symbols loaded` : 'Awaiting pipeline run';

        if (elements.rocCountDisplay) {
            elements.rocCountDisplay.textContent = `${completed} / ${total}`;
        }
        elements.completedBadge.textContent = `${completed} Calculated`;
        elements.completedBadge.className = completed > 0 ? 'badge badge-success' : 'badge badge-neutral';

        elements.downloadReportBtn.disabled = total === 0;

        if (elements.tableRowCount) {
            elements.tableRowCount.textContent = `Showing ${state.filteredRecords.length} of ${total} records`;
        }
    }

    function renderTable() {
        const tbody = elements.analysisTableBody;
        tbody.innerHTML = '';

        if (state.records.length === 0) {
            elements.emptyState.classList.remove('hidden');
            elements.noResultsState.classList.add('hidden');
            elements.analysisTable.classList.add('hidden');
            return;
        }

        elements.emptyState.classList.add('hidden');

        if (state.filteredRecords.length === 0) {
            elements.noResultsState.classList.remove('hidden');
            elements.analysisTable.classList.add('hidden');
            return;
        }

        elements.noResultsState.classList.add('hidden');
        elements.analysisTable.classList.remove('hidden');

        state.filteredRecords.forEach((record, index) => {
            const tr = createTableRow(record, index + 1);
            tbody.appendChild(tr);
        });
    }

    function createTableRow(record, rowNum) {
        const tr = document.createElement('tr');
        tr.id = `row-${record.symbol}`;
        if (record.status === 'incomplete_data') tr.classList.add('row-incomplete');
        if (record.status === 'error') tr.classList.add('row-error');

        // 1. Row index
        const tdIdx = document.createElement('td');
        tdIdx.className = 'text-muted font-mono text-sm';
        tdIdx.textContent = rowNum.toString();

        // 2. Symbol
        const tdSym = document.createElement('td');
        tdSym.className = 'symbol-cell';
        tdSym.textContent = record.symbol;

        // 3. Company Name
        const tdName = document.createElement('td');
        tdName.className = 'company-name-cell';
        tdName.textContent = record.company_name || record.symbol;
        tdName.title = record.company_name || record.symbol;

        // 4. Capital Employed with standard tooltip
        const tdCapEmp = document.createElement('td');
        if (record.capital_employed !== null && record.capital_employed !== undefined) {
            const taFormatted = formatNumber(record.total_assets);
            const tclFormatted = formatNumber(record.total_current_liabilities);
            const ceFormatted = formatNumber(record.capital_employed);

            const span = document.createElement('span');
            span.className = 'num-cell tooltip-wrapper';
            span.textContent = ceFormatted;
            span.setAttribute('data-tooltip', `Total Assets: ${taFormatted} | Current Liab: ${tclFormatted}`);
            tdCapEmp.appendChild(span);
        } else {
            const span = document.createElement('span');
            span.className = 'placeholder-cell';
            span.textContent = '—';
            if (record.error_message) {
                span.className += ' tooltip-wrapper';
                span.setAttribute('data-tooltip', record.error_message);
            }
            tdCapEmp.appendChild(span);
        }

        // 5. Select Expenses Button / Control
        const tdExpenses = document.createElement('td');
        tdExpenses.style.textAlign = 'center';
        const expItems = record.expense_items || [];
        const hasExpenses = expItems.length > 0;

        const btnExp = document.createElement('button');
        btnExp.type = 'button';
        btnExp.className = 'btn btn-secondary btn-sm';
        btnExp.disabled = !hasExpenses;

        const selectedCount = expItems.filter(i => i.selected).length;
        let expHtml = '';
        if (selectedCount > 0) {
            expHtml = `<span class="badge badge-primary" style="padding:0.1rem 0.4rem;">${selectedCount}</span> <span>Expenses</span>`;
        } else {
            expHtml = `<span>Select (${expItems.length})</span>`;
        }
        btnExp.innerHTML = expHtml;
        btnExp.addEventListener('click', () => openExpenseModal(record.symbol));
        tdExpenses.appendChild(btnExp);

        // 6. Operating Profit
        const tdOpProfit = document.createElement('td');
        tdOpProfit.id = `op-profit-${record.symbol}`;
        if (record.operating_profit !== null && record.operating_profit !== undefined) {
            const span = document.createElement('span');
            span.className = 'num-cell font-mono';
            span.textContent = formatNumber(record.operating_profit);
            tdOpProfit.appendChild(span);
        } else {
            const span = document.createElement('span');
            span.className = 'placeholder-cell';
            span.textContent = hasExpenses ? 'Select expenses to calculate' : (record.error_message || 'Incomplete data');
            tdOpProfit.appendChild(span);
        }

        // 7. ROC (%)
        const tdRoc = document.createElement('td');
        tdRoc.id = `roc-${record.symbol}`;
        tdRoc.style.textAlign = 'right';
        const rocVal = (record.roc !== null && record.roc !== undefined) ? record.roc : record.roce;
        if (rocVal !== null && rocVal !== undefined) {
            const badge = document.createElement('span');
            badge.className = `roc-badge ${rocVal >= 0 ? 'roc-positive' : 'roc-negative'}`;
            badge.textContent = `${formatNumber(rocVal, 2)}%`;
            tdRoc.appendChild(badge);
        } else {
            const span = document.createElement('span');
            span.className = 'placeholder-cell';
            span.textContent = hasExpenses ? 'Auto-calculating' : '—';
            tdRoc.appendChild(span);
        }

        tr.appendChild(tdIdx);
        tr.appendChild(tdSym);
        tr.appendChild(tdName);
        tr.appendChild(tdCapEmp);
        tr.appendChild(tdExpenses);
        tr.appendChild(tdOpProfit);
        tr.appendChild(tdRoc);

        return tr;
    }

    // -----------------------------------------------------------------------
    // Interactive Modal: Expense Selection & In-Place Update
    // -----------------------------------------------------------------------
    function openExpenseModal(symbol) {
        const record = state.records.find(r => r.symbol.toUpperCase() === symbol.toUpperCase());
        if (!record) return;

        state.currentModalRecord = record;
        elements.modalSymbolBadge.textContent = record.symbol;
        elements.modalCompanyName.textContent = record.company_name || record.symbol;

        elements.modalRevenueVal.textContent = formatNumber(record.revenue_from_operations);
        elements.modalCapEmployedVal.textContent = formatNumber(record.capital_employed);

        const listContainer = elements.modalExpensesList;
        listContainer.innerHTML = '';

        const items = record.expense_items || [];
        items.forEach((item, idx) => {
            const row = document.createElement('div');
            row.className = `expense-item-row ${item.selected ? 'selected' : ''}`;

            const labelGroup = document.createElement('div');
            labelGroup.className = 'expense-label-group';

            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.id = `exp-check-${idx}`;
            checkbox.className = 'expense-checkbox';
            checkbox.checked = !!item.selected;
            checkbox.dataset.name = item.name;
            checkbox.dataset.value = item.value || 0;

            const nameSpan = document.createElement('label');
            nameSpan.htmlFor = `exp-check-${idx}`;
            nameSpan.className = 'expense-name';
            nameSpan.textContent = item.name;

            labelGroup.appendChild(checkbox);
            labelGroup.appendChild(nameSpan);

            const valSpan = document.createElement('span');
            valSpan.className = 'expense-value';
            valSpan.textContent = formatNumber(item.value);

            row.appendChild(labelGroup);
            row.appendChild(valSpan);

            checkbox.addEventListener('change', () => {
                row.classList.toggle('selected', checkbox.checked);
                updateModalPreview();
            });

            listContainer.appendChild(row);
        });

        updateModalPreview();
        if (elements.mainContent) elements.mainContent.classList.add('blurred');
        elements.expenseModal.classList.remove('hidden');
    }

    function closeExpenseModal() {
        elements.expenseModal.classList.add('hidden');
        if (!state.isPolling && elements.mainContent && (!elements.confirmRefreshModal || elements.confirmRefreshModal.classList.contains('hidden'))) {
            elements.mainContent.classList.remove('blurred');
        }
        state.currentModalRecord = null;
    }

    function updateModalPreview() {
        if (!state.currentModalRecord) return;
        const checkboxes = elements.modalExpensesList.querySelectorAll('.expense-checkbox:checked');
        let totalExpenses = 0;
        checkboxes.forEach(cb => {
            totalExpenses += parseFloat(cb.dataset.value || 0);
        });

        elements.modalSelectedTotalVal.textContent = formatNumber(totalExpenses);

        const rev = state.currentModalRecord.revenue_from_operations;
        const capEmp = state.currentModalRecord.capital_employed;

        if (rev !== null && rev !== undefined) {
            const opProfit = Number(rev) - totalExpenses;
            elements.modalPreviewOpProfit.textContent = formatNumber(opProfit);

            if (capEmp && Number(capEmp) !== 0) {
                const roc = (opProfit / Number(capEmp)) * 100;
                if (elements.modalPreviewRoc) elements.modalPreviewRoc.textContent = `${formatNumber(roc, 2)}%`;
            } else {
                if (elements.modalPreviewRoc) elements.modalPreviewRoc.textContent = 'N/A';
            }
        } else {
            elements.modalPreviewOpProfit.textContent = '—';
            if (elements.modalPreviewRoc) elements.modalPreviewRoc.textContent = '—';
        }
    }

    async function saveModalExpenseSelection() {
        if (!state.currentModalRecord) return;
        const symbol = state.currentModalRecord.symbol;
        const checkboxes = elements.modalExpensesList.querySelectorAll('.expense-checkbox:checked');
        const selectedNames = Array.from(checkboxes).map(cb => cb.dataset.name);

        elements.modalCalculateBtn.disabled = true;
        elements.modalCalculateBtn.innerHTML = '<span class="spinner-small"></span> <span>Calculating...</span>';

        try {
            const resp = await fetch(`/api/analysis/${encodeURIComponent(symbol)}/select-expenses`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ selected_expenses: selectedNames }),
            });

            if (!resp.ok) {
                const errData = await resp.json();
                throw new Error(errData.error || `HTTP ${resp.status}`);
            }

            const updatedRecord = await resp.json();

            // Update in local state
            const idx = state.records.findIndex(r => r.symbol.toUpperCase() === symbol.toUpperCase());
            if (idx !== -1) {
                state.records[idx] = updatedRecord;
            }

            // Update in place without full page reload
            updateSingleRowInPlace(updatedRecord);
            updateStats();
            closeExpenseModal();
            const resRoc = (updatedRecord.roc !== null && updatedRecord.roc !== undefined) ? updatedRecord.roc : updatedRecord.roce;
            showToast(`Calculated ROC for ${symbol}: ${formatNumber(resRoc, 2)}%`);

        } catch (err) {
            console.error('Failed to update expenses:', err);
            alert(`Error calculating ROC: ${err.message}`);
        } finally {
            elements.modalCalculateBtn.disabled = false;
            elements.modalCalculateBtn.innerHTML = '<span>Calculate ROC</span>';
        }
    }

    function updateSingleRowInPlace(record) {
        const row = document.getElementById(`row-${record.symbol}`);
        if (!row) {
            filterAndRender();
            return;
        }

        // Update Expenses button badge
        const tdExpenses = row.children[4];
        if (tdExpenses) {
            const expBtn = tdExpenses.querySelector('button');
            const selectedCount = (record.expense_items || []).filter(i => i.selected).length;
            if (expBtn) {
                let expHtml = '';
                if (selectedCount > 0) {
                    expHtml = `<span class="badge badge-primary" style="padding:0.1rem 0.4rem;">${selectedCount}</span> <span>Expenses</span>`;
                } else {
                    expHtml = `<span>Select (${(record.expense_items || []).length})</span>`;
                }
                expBtn.innerHTML = expHtml;
            }
        }

        // Update Operating Profit
        const tdOpProfit = document.getElementById(`op-profit-${record.symbol}`);
        if (tdOpProfit) {
            tdOpProfit.innerHTML = '';
            if (record.operating_profit !== null && record.operating_profit !== undefined) {
                const span = document.createElement('span');
                span.className = 'num-cell font-mono';
                span.textContent = formatNumber(record.operating_profit);
                tdOpProfit.appendChild(span);
            } else {
                const span = document.createElement('span');
                span.className = 'placeholder-cell';
                span.textContent = 'Select expenses to calculate';
                tdOpProfit.appendChild(span);
            }
        }

        // Update ROC
        const tdRoc = document.getElementById(`roc-${record.symbol}`) || document.getElementById(`roce-${record.symbol}`);
        if (tdRoc) {
            tdRoc.innerHTML = '';
            const rocVal = (record.roc !== null && record.roc !== undefined) ? record.roc : record.roce;
            if (rocVal !== null && rocVal !== undefined) {
                const badge = document.createElement('span');
                badge.className = `roc-badge ${rocVal >= 0 ? 'roc-positive' : 'roc-negative'}`;
                badge.textContent = `${formatNumber(rocVal, 2)}%`;
                tdRoc.appendChild(badge);
            } else {
                const span = document.createElement('span');
                span.className = 'placeholder-cell';
                span.textContent = '—';
                tdRoc.appendChild(span);
            }
        }
    }

    // -----------------------------------------------------------------------
    // Centered Progress Overlay & Polling Logic
    // -----------------------------------------------------------------------
    function showProgressOverlay() {
        if (elements.mainContent) elements.mainContent.classList.add('blurred');
        if (elements.progressOverlay) elements.progressOverlay.classList.remove('hidden');
        if (elements.progressBarFill) elements.progressBarFill.style.width = '0%';
        if (elements.progressPercent) elements.progressPercent.textContent = '0%';
        if (elements.progressStep) elements.progressStep.textContent = 'Starting...';

        elements.refreshBtn.disabled = true;
        if (elements.statusDot) elements.statusDot.className = 'status-dot busy';
        if (elements.statusText) elements.statusText.textContent = 'Pipeline Active';
    }

    function hideProgressOverlay() {
        if (elements.mainContent) elements.mainContent.classList.remove('blurred');
        if (elements.progressOverlay) elements.progressOverlay.classList.add('hidden');

        elements.refreshBtn.disabled = false;
        if (elements.statusDot) elements.statusDot.className = 'status-dot online';
        if (elements.statusText) elements.statusText.textContent = 'System Ready';
    }

    function updateProgressUI(progress) {
        const pct = progress.percent !== undefined ? Math.max(0, Math.min(100, progress.percent)) : 0;
        if (elements.progressBarFill) elements.progressBarFill.style.width = `${pct}%`;
        if (elements.progressPercent) elements.progressPercent.textContent = `${pct}%`;

        let stepText = progress.current_step || 'Processing...';
        if (progress.current_symbol && !stepText.includes(progress.current_symbol)) {
            stepText = `${stepText} (${progress.current_symbol})`;
        }
        if (elements.progressStep) elements.progressStep.textContent = stepText;
    }

    // -----------------------------------------------------------------------
    // Confirmation Modal Handlers
    // -----------------------------------------------------------------------
    function openConfirmRefreshModal() {
        if (elements.mainContent) elements.mainContent.classList.add('blurred');
        if (elements.confirmRefreshModal) elements.confirmRefreshModal.classList.remove('hidden');
    }

    function closeConfirmRefreshModal() {
        if (elements.confirmRefreshModal) elements.confirmRefreshModal.classList.add('hidden');
        if (!state.isPolling && elements.mainContent && (!elements.expenseModal || elements.expenseModal.classList.contains('hidden'))) {
            elements.mainContent.classList.remove('blurred');
        }
    }

    function onRefreshClick() {
        openConfirmRefreshModal();
    }

    async function onConfirmRefreshExecute() {
        // Hide confirmation modal while retaining blurred background for progress overlay
        if (elements.confirmRefreshModal) elements.confirmRefreshModal.classList.add('hidden');

        hideError();
        showProgressOverlay();

        try {
            const resp = await fetch('/api/refresh/full-pipeline', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
            });

            if (resp.status === 202 || resp.status === 200) {
                startPollingProgress();
            } else if (resp.status === 409) {
                showToast('Pipeline is already executing. Connecting live progress...');
                startPollingProgress();
            } else {
                const err = await resp.json();
                throw new Error(err.message || err.error || `HTTP ${resp.status}`);
            }
        } catch (err) {
            console.error('Failed to trigger pipeline:', err);
            showError('Pipeline Launch Failed', err.message);
            hideProgressOverlay();
        }
    }

    function startPollingProgress() {
        if (state.isPolling) return;
        state.isPolling = true;
        state.pollFailures = 0;
        showProgressOverlay();

        if (state.pollIntervalId) clearInterval(state.pollIntervalId);

        state.pollIntervalId = setInterval(async () => {
            try {
                const resp = await fetch('/api/progress', {
                    headers: { 'Cache-Control': 'no-cache' }
                });
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const progress = await resp.json();
                state.pollFailures = 0;

                updateProgressUI(progress);

                if (!progress.running) {
                    // Show 100% / Finalizing briefly for a satisfying finish
                    if (elements.progressBarFill) elements.progressBarFill.style.width = '100%';
                    if (elements.progressPercent) elements.progressPercent.textContent = '100%';
                    if (elements.progressStep) elements.progressStep.textContent = progress.status === 'error' ? 'Pipeline error' : 'Finalizing results...';

                    stopPollingProgress();

                    setTimeout(async () => {
                        hideProgressOverlay();
                        if (progress.status === 'error') {
                            showError('Pipeline Error', progress.error || 'An error occurred during execution.');
                        } else {
                            showToast('Financial analysis pipeline completed successfully!');
                        }
                        await fetchAnalysisData();
                    }, 600);
                }
            } catch (err) {
                console.error('Progress polling error:', err);
                state.pollFailures++;
                if (state.pollFailures >= 3 && elements.progressStep) {
                    elements.progressStep.textContent = 'Lost connection to server, retrying...';
                }
            }
        }, 1000);
    }

    function stopPollingProgress() {
        state.isPolling = false;
        if (state.pollIntervalId) {
            clearInterval(state.pollIntervalId);
            state.pollIntervalId = null;
        }
    }

    // -----------------------------------------------------------------------
    // Excel Export
    // -----------------------------------------------------------------------
    function onDownloadReportClick() {
        window.location.href = '/api/analysis/export';
    }

    // -----------------------------------------------------------------------
    // Initial Setup & Event Listeners
    // -----------------------------------------------------------------------
    function init() {
        // Event Listeners
        elements.refreshBtn.addEventListener('click', onRefreshClick);
        elements.downloadReportBtn.addEventListener('click', onDownloadReportClick);

        elements.searchInput.addEventListener('input', (e) => {
            elements.clearSearchBtn.classList.toggle('hidden', !e.target.value);
            filterAndRender();
        });

        elements.clearSearchBtn.addEventListener('click', () => {
            elements.searchInput.value = '';
            elements.clearSearchBtn.classList.add('hidden');
            filterAndRender();
        });

        elements.closeErrorBtn.addEventListener('click', hideError);

        // Confirmation Modal Listeners
        if (elements.confirmRefreshCancel) {
            elements.confirmRefreshCancel.addEventListener('click', closeConfirmRefreshModal);
        }
        if (elements.confirmRefreshOk) {
            elements.confirmRefreshOk.addEventListener('click', onConfirmRefreshExecute);
        }
        if (elements.confirmRefreshModal) {
            elements.confirmRefreshModal.addEventListener('click', (e) => {
                if (e.target === elements.confirmRefreshModal) {
                    closeConfirmRefreshModal();
                }
            });
        }

        // Expense Modal Listeners
        elements.modalCloseBtn.addEventListener('click', closeExpenseModal);
        elements.modalCancelBtn.addEventListener('click', closeExpenseModal);
        elements.modalCalculateBtn.addEventListener('click', saveModalExpenseSelection);

        if (elements.expenseModal) {
            elements.expenseModal.addEventListener('click', (e) => {
                if (e.target === elements.expenseModal) {
                    closeExpenseModal();
                }
            });
        }

        elements.modalSelectAllBtn.addEventListener('click', () => {
            const checkboxes = elements.modalExpensesList.querySelectorAll('.expense-checkbox');
            checkboxes.forEach(cb => {
                cb.checked = true;
                cb.closest('.expense-item-row').classList.add('selected');
            });
            updateModalPreview();
        });

        elements.modalClearAllBtn.addEventListener('click', () => {
            const checkboxes = elements.modalExpensesList.querySelectorAll('.expense-checkbox');
            checkboxes.forEach(cb => {
                cb.checked = false;
                cb.closest('.expense-item-row').classList.remove('selected');
            });
            updateModalPreview();
        });

        // Close modal on escape key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                if (elements.confirmRefreshModal && !elements.confirmRefreshModal.classList.contains('hidden')) {
                    closeConfirmRefreshModal();
                } else if (elements.expenseModal && !elements.expenseModal.classList.contains('hidden')) {
                    closeExpenseModal();
                }
            }
        });

        // Check if a pipeline is already running on page load
        fetch('/api/progress')
            .then(res => res.json())
            .then(prog => {
                if (prog.running) {
                    startPollingProgress();
                }
            })
            .catch(() => {});

        // Fetch initial dataset
        fetchAnalysisData();
    }

    // Start application on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
