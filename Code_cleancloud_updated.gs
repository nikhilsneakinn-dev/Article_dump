/**
 * CleanCloud -> Google Sheets sync for CC-Orders-17032021-19052026.
 *
 * Active flows:
 * - Sync recent changed/new orders
 * - Sync not-collected/open order status and receipt links
 * - Backfill delivery fields from summary without API usage
 *
 * Removed:
 * - Completed test-window sync for orders 81272-81431
 * - Manual Enrich 20 Customers action
 */

const CONFIG = {
  spreadsheetId: '1v1pwR8yCx4o8mZULOO3OKJyMl-IPRPnFRdjdqk8SP9M',
  cleanCloudBaseUrl: 'https://cleancloudapp.com',
  cleanCloudApiBaseUrl: 'https://cleancloudapp.com/api',
  timezone: 'Asia/Calcutta',
  maxRequestsPerRun: 300,
  maxCustomerRequestsPerRun: 20,
  customerRefreshDays: 7,
  throttleMs: 400,
  rocketButtonGifUrl: '',
  rocketButtonCell: { column: 2, row: 2 },
  openOrderDateFrom: '2026-05-01',
  openDateRequestLimit: 25,
  openDateMinRows: 3,
  openOrderFallbackLimit: 50,
  recentUpdatedSecondsAgoFrom: 172800,
  sheets: {
    orders: 'CC-Orders-17032021-19052026',
    apiCache: '_API_Cache',
    customerCache: '_Customer_Cache',
    statusMap: '_Status_Map',
    staffMap: '_Staff_Map',
    paymentMap: '_Payment_Map',
    syncControl: '_Sync_Control',
    syncLog: '_Sync_Log',
  },
};

const ORDER_HEADERS = [
  'id',
  'createdDate',
  'staffIds.create',
  'cleanedDate',
  'staffIds.cleaned',
  'completedDate',
  'staffIds.completed',
  'customer',
  'customerID',
  'phone',
  'address',
  'pieces',
  'summary',
  'deliveryType',
  'deliveryArea',
  'total',
  'status',
  'receiptLink',
  'apiLastChecked',
  'apiChangeStatus',
  'apiRawHash',
  'invoiceUniqueID',
  'invoiceStatus',
  'paid',
  'paymentType',
  'paymentTime',
  'tax1',
  'tax2',
  'tax3',
];

const API_CACHE_HEADERS = [
  'id',
  'customerID',
  'invoiceUniqueID',
  'receiptLink',
  'cleancloudUpdatedAt',
  'apiRawHash',
  'lastSeenAt',
  'syncStatus',
  'sourceEndpoint',
  'requestPage',
  'matchConfidence',
  'rawJsonRef',
];

const CUSTOMER_CACHE_HEADERS = [
  'ID',
  'Name',
  'Email',
  'Tel',
  'Address',
  'addressDetailed.street',
  'addressDetailed.city',
  'customerAddressInstructions',
  'Notes',
  'signedUpTimeStamp',
  'marketingOptIn',
  'businessID',
  'taxID',
  'apiRawHash',
  'lastSeenAt',
  'syncStatus',
];

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('🚀 Sneakinn')
    .addItem('🔄 Sync Ready Orders', 'syncReadyOrders')
    .addItem('👥 Run Assignment', 'runAssignment')
    .addSeparator()
    .addItem('Set CleanCloud Token', 'setCleanCloudToken')
    .addItem('Check Changes Now', 'checkForChangesButton')
    .addItem('Sync Not Collected Status + Links', 'syncNotCollectedStatusAndLinks')
    .addItem('Backfill Delivery Fields', 'backfillDeliveryFieldsFromSummary')
    .addItem('Add Rocket Check Button', 'addRocketCheckButton')
    .addToUi();
}

function syncReadyOrders() {
  return checkForChangesButton();
}

function runAssignment() {
  SpreadsheetApp.getUi().alert('Run Assignment is ready in the menu, but assignment logic has not been configured yet.');
}

function checkForChangesButton() {
  return syncRecentChangesAndNewOrders();
}

function addRocketCheckButton() {
  if (!CONFIG.rocketButtonGifUrl) {
    SpreadsheetApp.getUi().alert('Add the Sneakinn rocket GIF URL in CONFIG.rocketButtonGifUrl first.');
    return;
  }

  const ss = SpreadsheetApp.openById(CONFIG.spreadsheetId);
  const sheet = mustGetSheet_(ss, CONFIG.sheets.orders);
  const image = sheet.insertImage(CONFIG.rocketButtonGifUrl, CONFIG.rocketButtonCell.column, CONFIG.rocketButtonCell.row);
  image.assignScript('checkForChangesButton');
  image.setAltTextTitle('Check CleanCloud changes');
  image.setAltTextDescription('Click to sync recent CleanCloud changes and new orders.');
  SpreadsheetApp.getUi().alert('Rocket check button added. Click the GIF to check for changes.');
}

function setCleanCloudToken() {
  const ui = SpreadsheetApp.getUi();
  const response = ui.prompt('CleanCloud API Token', 'Paste the API token. It will be stored in Script Properties, not the sheet.', ui.ButtonSet.OK_CANCEL);
  if (response.getSelectedButton() !== ui.Button.OK) return;
  const token = response.getResponseText().trim();
  if (!token) throw new Error('No token entered.');
  PropertiesService.getScriptProperties().setProperty('CLEANCLOUD_API_TOKEN', token);
  ui.alert('Token saved.');
}

function backfillDeliveryFieldsFromSummary() {
  const ss = SpreadsheetApp.openById(CONFIG.spreadsheetId);
  const ordersSheet = mustGetSheet_(ss, CONFIG.sheets.orders);
  assertHeaders_(ordersSheet, ORDER_HEADERS);

  const headerMap = getHeaderMap_(ordersSheet);
  const lastRow = ordersSheet.getLastRow();
  if (lastRow < 2) return { rowsChecked: 0, rowsUpdated: 0, apiRequestsUsed: 0 };

  const chunkSize = 5000;
  let rowsChecked = 0;
  let rowsUpdated = 0;

  for (let startRow = 2; startRow <= lastRow; startRow += chunkSize) {
    const numRows = Math.min(chunkSize, lastRow - startRow + 1);
    const summaryValues = ordersSheet.getRange(startRow, headerMap.summary + 1, numRows, 1).getValues();
    const output = summaryValues.map(row => {
      const delivery = extractDeliveryFields_(row[0]);
      return [delivery.type, delivery.area];
    });
    ordersSheet.getRange(startRow, headerMap.deliveryType + 1, numRows, 2).setValues(output);
    rowsChecked += numRows;
    rowsUpdated += output.filter(row => row[0] || row[1]).length;
  }

  const summary = { rowsChecked, rowsUpdated, apiRequestsUsed: 0 };
  SpreadsheetApp.getUi().alert(JSON.stringify(summary, null, 2));
  return summary;
}

function syncRecentChangesAndNewOrders() {
  const startedAt = new Date();
  const ss = SpreadsheetApp.openById(CONFIG.spreadsheetId);
  const ordersSheet = mustGetSheet_(ss, CONFIG.sheets.orders);
  const apiCacheSheet = mustGetSheet_(ss, CONFIG.sheets.apiCache);
  const customerCacheSheet = mustGetSheet_(ss, CONFIG.sheets.customerCache);
  const statusMapSheet = mustGetSheet_(ss, CONFIG.sheets.statusMap);
  const staffMapSheet = mustGetSheet_(ss, CONFIG.sheets.staffMap);
  const paymentMapSheet = mustGetSheet_(ss, CONFIG.sheets.paymentMap);
  const logSheet = mustGetSheet_(ss, CONFIG.sheets.syncLog);
  const requestState = { used: 0, customerUsed: 0, errors: [] };

  try {
    assertHeaders_(ordersSheet, ORDER_HEADERS);
    assertHeaders_(apiCacheSheet, API_CACHE_HEADERS);
    assertHeaders_(customerCacheSheet, CUSTOMER_CACHE_HEADERS);

    const orderHeaderMap = getHeaderMap_(ordersSheet);
    const existingOrderRows = getIdToRowMap_(ordersSheet, orderHeaderMap.id);
    const customerCache = loadCustomerCache_(customerCacheSheet);
    const statusMap = loadStatusMap_(statusMapSheet);
    const staffMap = loadSimpleMap_(staffMapSheet, 'staffID', 'staffName');
    const paymentMap = loadPaymentMap_(paymentMapSheet);
    const nowText = formatDateTime_(new Date());

    const response = cleanCloudPost_('getOrders', {
      updatedSecondsAgoFrom: CONFIG.recentUpdatedSecondsAgoFrom,
      sendProductDetails: 0,
    }, requestState);
    const changedOrders = normalizeArray_(response.Orders || response.orders || response.Order || response.order)
      .sort((a, b) => numberValue_(a.id) - numberValue_(b.id));

    const changedCustomerIds = unique_(changedOrders
      .map(order => String(order.customerID || '').trim())
      .filter(customerId => customerId));
    const customerIdsToFetch = changedCustomerIds
      .filter(customerId => !customerCache[customerId] || isCustomerCacheStale_(customerCache[customerId], CONFIG.customerRefreshDays))
      .slice(0, CONFIG.maxCustomerRequestsPerRun);
    const fetchedCustomers = fetchCustomers_(customerIdsToFetch, requestState);
    const refreshedCustomerIds = new Set(Object.keys(fetchedCustomers));
    Object.keys(fetchedCustomers).forEach(customerId => {
      customerCache[customerId] = fetchedCustomers[customerId];
    });

    const appendRows = [];
    const updateJobs = [];
    const cacheRows = [];
    let rowsUpdated = 0;
    let rowsAppended = 0;
    let receiptLinksFilled = 0;

    changedOrders.forEach(order => {
      const customer = customerCache[String(order.customerID || '')] || {};
      const rowValues = buildOrderRow_(order, customer, statusMap, staffMap, paymentMap, nowText);
      const rowHash = rowValues[ORDER_HEADERS.indexOf('apiRawHash')];
      const existingRow = existingOrderRows[String(order.id)];
      if (existingRow) {
        const currentHash = ordersSheet.getRange(existingRow, orderHeaderMap.apiRawHash + 1).getDisplayValue();
        const currentValues = ordersSheet.getRange(existingRow, 1, 1, ORDER_HEADERS.length).getValues()[0];
        preserveExistingWhenBlank_(rowValues, currentValues, orderHeaderMap, ['customer', 'phone', 'address']);
        const customerWasRefreshed = refreshedCustomerIds.has(String(order.customerID || '').trim());
        const customerFieldsChanged = customerWasRefreshed && haveFieldsChanged_(rowValues, currentValues, orderHeaderMap, ['customer', 'phone', 'address']);
        if (currentHash !== rowHash || customerFieldsChanged) {
          if (!currentValues[orderHeaderMap.receiptLink] && rowValues[orderHeaderMap.receiptLink]) receiptLinksFilled++;
          updateJobs.push({ row: existingRow, values: rowValues });
          rowsUpdated++;
        }
      } else {
        if (rowValues[orderHeaderMap.receiptLink]) receiptLinksFilled++;
        appendRows.push(rowValues);
        rowsAppended++;
      }
      cacheRows.push(buildApiCacheRow_(order, rowHash, nowText));
    });

    writeRowJobs_(ordersSheet, updateJobs, ORDER_HEADERS.length);
    if (appendRows.length) {
      ordersSheet.getRange(ordersSheet.getLastRow() + 1, 1, appendRows.length, ORDER_HEADERS.length).setValues(appendRows);
    }
    appendToCache_(apiCacheSheet, cacheRows, API_CACHE_HEADERS.length);
    appendToCache_(customerCacheSheet, Object.keys(fetchedCustomers).map(customerId => buildCustomerCacheRow_(fetchedCustomers[customerId], nowText)), CUSTOMER_CACHE_HEADERS.length);

    appendSyncLog_(logSheet, {
      runAt: nowText,
      runType: 'sync_recent_changes_new_orders',
      requestsUsed: requestState.used,
      recordsPulled: changedOrders.length,
      rowsUpdated,
      rowsAppended,
      receiptLinksFilled,
      errors: requestState.errors.join(' | '),
      nextSince: '',
      notes: `updatedSecondsAgoFrom=${CONFIG.recentUpdatedSecondsAgoFrom}; customerRefreshDays=${CONFIG.customerRefreshDays}; changedCustomerIds=${changedCustomerIds.length}; customersFetched=${Object.keys(fetchedCustomers).length}; durationSec=${Math.round((new Date() - startedAt) / 1000)}`,
    });

    const summary = {
      updatedSecondsAgoFrom: CONFIG.recentUpdatedSecondsAgoFrom,
      requestsUsed: requestState.used,
      recordsPulled: changedOrders.length,
      rowsUpdated,
      rowsAppended,
      receiptLinksFilled,
      customersFetched: Object.keys(fetchedCustomers).length,
      errors: requestState.errors,
    };
    Logger.log(JSON.stringify(summary, null, 2));
    SpreadsheetApp.getUi().alert(JSON.stringify(summary, null, 2));
    return summary;
  } catch (error) {
    appendFailedSyncLog_(logSheet, 'sync_recent_changes_new_orders_failed', requestState, startedAt, error);
    throw error;
  }
}

function syncNotCollectedStatusAndLinks() {
  const startedAt = new Date();
  const ss = SpreadsheetApp.openById(CONFIG.spreadsheetId);
  const ordersSheet = mustGetSheet_(ss, CONFIG.sheets.orders);
  const apiCacheSheet = mustGetSheet_(ss, CONFIG.sheets.apiCache);
  const statusMapSheet = mustGetSheet_(ss, CONFIG.sheets.statusMap);
  const staffMapSheet = mustGetSheet_(ss, CONFIG.sheets.staffMap);
  const paymentMapSheet = mustGetSheet_(ss, CONFIG.sheets.paymentMap);
  const logSheet = mustGetSheet_(ss, CONFIG.sheets.syncLog);
  const requestState = { used: 0, customerUsed: 0, errors: [] };
  const nowText = formatDateTime_(new Date());

  try {
    assertHeaders_(ordersSheet, ORDER_HEADERS);
    assertHeaders_(apiCacheSheet, API_CACHE_HEADERS);

    const orderHeaderMap = getHeaderMap_(ordersSheet);
    const statusMap = loadStatusMap_(statusMapSheet);
    const staffMap = loadSimpleMap_(staffMapSheet, 'staffID', 'staffName');
    const paymentMap = loadPaymentMap_(paymentMapSheet);
    const openOrderRows = getNotCollectedOrderRows_(ordersSheet, orderHeaderMap);
    const openIds = openOrderRows.map(item => item.id);
    const openIdSet = new Set(openIds.map(String));
    const rowById = {};
    openOrderRows.forEach(item => {
      rowById[String(item.id)] = item.row;
    });

    const fetchedById = {};
    fetchOrdersByOpenDates_(openOrderRows, requestState).forEach(order => {
      const id = String(order.id || '');
      if (id && openIdSet.has(id)) fetchedById[id] = order;
    });

    const missingIds = openIds
      .filter(id => !fetchedById[String(id)])
      .slice(0, CONFIG.openOrderFallbackLimit);

    fetchOrdersById_(missingIds, requestState).forEach(order => {
      const id = String(order.id || '');
      if (id && openIdSet.has(id)) fetchedById[id] = order;
    });

    const updateJobs = [];
    const cacheRows = [];
    let receiptLinksFilled = 0;
    const currentRowsById = loadRowsForIds_(ordersSheet, rowById, Object.keys(fetchedById), ORDER_HEADERS.length);
    Object.keys(fetchedById).forEach(id => {
      const order = fetchedById[id];
      const row = rowById[id];
      const mappedStatus = mapStatus_(order.status, statusMap);
      const receiptLink = fullReceiptLink_(order.receiptLink || '');
      const rowHash = md5_(JSON.stringify(order));

      const currentValues = currentRowsById[id] || ordersSheet.getRange(row, 1, 1, ORDER_HEADERS.length).getValues()[0];
      const oldReceiptLink = currentValues[orderHeaderMap.receiptLink];
      currentValues[orderHeaderMap.status] = mappedStatus;
      currentValues[orderHeaderMap['staffIds.create']] = mapStaff_(nestedValue_(order, 'staffIds.create'), staffMap);
      currentValues[orderHeaderMap['staffIds.cleaned']] = mapStaff_(nestedValue_(order, 'staffIds.cleaned'), staffMap);
      currentValues[orderHeaderMap['staffIds.completed']] = mapStaff_(nestedValue_(order, 'staffIds.completed'), staffMap);
      currentValues[orderHeaderMap.receiptLink] = receiptLink;
      currentValues[orderHeaderMap.invoiceUniqueID] = stringOrBlank_(order.invoiceUniqueID);
      currentValues[orderHeaderMap.invoiceStatus] = stringOrBlank_(order.invoiceStatus);
      currentValues[orderHeaderMap.paid] = mapPaid_(order.paid, paymentMap);
      currentValues[orderHeaderMap.paymentType] = mapPaymentType_(order.paymentType, paymentMap);
      currentValues[orderHeaderMap.paymentTime] = unixToDateOrBlank_(order.paymentTime);
      currentValues[orderHeaderMap.tax1] = numberOrString_(order.tax1);
      currentValues[orderHeaderMap.tax2] = numberOrString_(order.tax2);
      currentValues[orderHeaderMap.tax3] = numberOrString_(order.tax3);
      currentValues[orderHeaderMap.apiLastChecked] = nowText;
      currentValues[orderHeaderMap.apiChangeStatus] = 'open_status_refreshed';
      currentValues[orderHeaderMap.apiRawHash] = rowHash;

      if (!oldReceiptLink && receiptLink) receiptLinksFilled++;
      updateJobs.push({ row, values: currentValues });
      cacheRows.push(buildApiCacheRow_(order, rowHash, nowText));
    });

    writeRowJobs_(ordersSheet, updateJobs, ORDER_HEADERS.length);
    appendToCache_(apiCacheSheet, cacheRows, API_CACHE_HEADERS.length);
    appendSyncLog_(logSheet, {
      runAt: nowText,
      runType: 'sync_not_collected_status_links',
      requestsUsed: requestState.used,
      recordsPulled: Object.keys(fetchedById).length,
      rowsUpdated: updateJobs.length,
      rowsAppended: 0,
      receiptLinksFilled,
      errors: requestState.errors.join(' | '),
      nextSince: '',
      notes: `openRows=${openIds.length}; dateRequestLimit=${CONFIG.openDateRequestLimit}; dateMinRows=${CONFIG.openDateMinRows}; fallbackLimit=${CONFIG.openOrderFallbackLimit}; fallbackUsed=${missingIds.length}; durationSec=${Math.round((new Date() - startedAt) / 1000)}`,
    });

    const summary = {
      openRowsInSheet: openIds.length,
      dateWindowRequestsPlusFallback: requestState.used,
      fetchedOpenOrders: Object.keys(fetchedById).length,
      rowsUpdated: updateJobs.length,
      receiptLinksFilled,
      fallbackIdsChecked: missingIds.length,
      stillNotChecked: Math.max(openIds.length - Object.keys(fetchedById).length, 0),
      errors: requestState.errors,
    };
    Logger.log(JSON.stringify(summary, null, 2));
    SpreadsheetApp.getUi().alert(JSON.stringify(summary, null, 2));
    return summary;
  } catch (error) {
    appendFailedSyncLog_(logSheet, 'sync_not_collected_status_links_failed', requestState, startedAt, error);
    throw error;
  }
}

function fetchOrdersById_(orderIds, requestState) {
  const orders = [];
  orderIds.forEach(orderId => {
    if (requestState.used >= CONFIG.maxRequestsPerRun) {
      requestState.errors.push(`Request cap hit before orderID ${orderId}`);
      return;
    }
    try {
      const response = cleanCloudPost_('getOrders', { orderID: String(orderId), sendProductDetails: 0 }, requestState);
      normalizeArray_(response.Orders || response.orders || response.Order || response.order).forEach(order => orders.push(order));
    } catch (error) {
      requestState.errors.push(`orderID ${orderId}: ${error.message}`);
    }
  });
  return orders;
}

function fetchOrdersByOpenDates_(openOrderRows, requestState) {
  const orders = [];
  const dateCounts = {};
  openOrderRows.forEach(item => {
    if (!item.createdDate) return;
    dateCounts[item.createdDate] = (dateCounts[item.createdDate] || 0) + 1;
  });
  const dates = Object.keys(dateCounts)
    .filter(dateText => dateCounts[dateText] >= CONFIG.openDateMinRows)
    .sort((a, b) => {
      const byCount = dateCounts[b] - dateCounts[a];
      return byCount || (a < b ? 1 : -1);
    })
    .slice(0, CONFIG.openDateRequestLimit);

  dates.forEach(dateText => {
    if (requestState.used >= CONFIG.maxRequestsPerRun) {
      requestState.errors.push(`Request cap hit before date ${dateText}`);
      return;
    }
    try {
      const response = cleanCloudPost_('getOrders', {
        dateFrom: dateText,
        dateTo: dateText,
        sendProductDetails: 0,
      }, requestState);
      normalizeArray_(response.Orders || response.orders || response.Order || response.order).forEach(order => orders.push(order));
    } catch (error) {
      requestState.errors.push(`date ${dateText}: ${error.message}`);
    }
  });
  return orders;
}

function fetchOrdersByStatusBuckets_(requestState) {
  const orders = [];
  const today = Utilities.formatDate(new Date(), CONFIG.timezone, 'yyyy-MM-dd');
  ['0', '1', '3', '4', '5', '2'].forEach(status => {
    if (requestState.used >= CONFIG.maxRequestsPerRun) {
      requestState.errors.push(`Request cap hit before status ${status}`);
      return;
    }
    try {
      const response = cleanCloudPost_('getOrders', {
        dateFrom: CONFIG.openOrderDateFrom,
        dateTo: today,
        status,
        sendProductDetails: 0,
      }, requestState);
      normalizeArray_(response.Orders || response.orders || response.Order || response.order).forEach(order => orders.push(order));
    } catch (error) {
      requestState.errors.push(`status ${status}: ${error.message}`);
    }
  });
  return orders;
}

function getNotCollectedOrderRows_(sheet, orderHeaderMap) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return [];
  const idCol = orderHeaderMap.id + 1;
  const createdDateCol = orderHeaderMap.createdDate + 1;
  const statusCol = orderHeaderMap.status + 1;
  const apiChangeStatusCol = orderHeaderMap.apiChangeStatus + 1;
  const minCol = Math.min(idCol, createdDateCol, statusCol, apiChangeStatusCol);
  const maxCol = Math.max(idCol, createdDateCol, statusCol, apiChangeStatusCol);
  const width = maxCol - minCol + 1;
  const values = sheet.getRange(2, minCol, lastRow - 1, width).getValues();
  const idOffset = idCol - minCol;
  const createdDateOffset = createdDateCol - minCol;
  const statusOffset = statusCol - minCol;
  const apiChangeStatusOffset = apiChangeStatusCol - minCol;
  const rows = [];
  values.forEach((row, index) => {
    const id = String(row[idOffset] || '').trim();
    const status = String(row[statusOffset] || '').trim().toLowerCase();
    const apiChangeStatus = String(row[apiChangeStatusOffset] || '').trim().toLowerCase();
    if (!id || id === 'id') return;
    if (apiChangeStatus === 'open_status_refreshed') return;
    if (status && status !== 'collected') {
      rows.push({ id, row: index + 2, status, createdDate: sheetDateToApiDate_(row[createdDateOffset]) });
    }
  });
  return rows;
}

function sheetDateToApiDate_(value) {
  if (!value) return '';
  if (Object.prototype.toString.call(value) === '[object Date]' && !isNaN(value.getTime())) {
    return Utilities.formatDate(value, CONFIG.timezone, 'yyyy-MM-dd');
  }
  const text = String(value).trim();
  const parsed = new Date(text);
  if (!isNaN(parsed.getTime())) return Utilities.formatDate(parsed, CONFIG.timezone, 'yyyy-MM-dd');
  return '';
}

function loadRowsForIds_(sheet, rowById, ids, width) {
  const result = {};
  const rows = ids
    .map(id => ({ id, row: rowById[id] }))
    .filter(item => item.row)
    .sort((a, b) => a.row - b.row);
  const groups = groupContiguousRows_(rows);
  groups.forEach(group => {
    const values = sheet.getRange(group.startRow, 1, group.count, width).getValues();
    group.items.forEach((item, index) => {
      result[item.id] = values[index];
    });
  });
  return result;
}

function writeRowJobs_(sheet, jobs, width) {
  if (!jobs.length) return;
  const sorted = jobs.slice().sort((a, b) => a.row - b.row);
  const groups = groupContiguousRows_(sorted.map(job => ({ id: String(job.row), row: job.row, values: job.values })));
  groups.forEach(group => {
    const values = group.items.map(item => item.values);
    sheet.getRange(group.startRow, 1, group.count, width).setValues(values);
  });
}

function groupContiguousRows_(items) {
  const groups = [];
  let current = null;
  items.forEach(item => {
    if (!current || item.row !== current.startRow + current.count) {
      current = { startRow: item.row, count: 0, items: [] };
      groups.push(current);
    }
    current.items.push(item);
    current.count++;
  });
  return groups;
}

function fetchCustomers_(customerIds, requestState) {
  const customersById = {};
  customerIds.forEach(customerId => {
    if (requestState.used >= CONFIG.maxRequestsPerRun) {
      requestState.errors.push(`Request cap hit before customerID ${customerId}`);
      return;
    }
    if (requestState.customerUsed >= CONFIG.maxCustomerRequestsPerRun) {
      requestState.errors.push(`Customer request cap hit before customerID ${customerId}`);
      return;
    }
    try {
      const customer = cleanCloudPost_('getCustomer', { customerID: String(customerId) }, requestState);
      requestState.customerUsed++;
      if (customer && customer.Success === 'True') {
        customersById[String(customer.ID || customerId)] = customer;
      } else {
        requestState.errors.push(`customerID ${customerId}: ${customer.Error || 'unknown error'}`);
      }
    } catch (error) {
      requestState.errors.push(`customerID ${customerId}: ${error.message}`);
    }
  });
  return customersById;
}

function cleanCloudPost_(endpoint, payload, requestState) {
  const token = PropertiesService.getScriptProperties().getProperty('CLEANCLOUD_API_TOKEN');
  if (!token) throw new Error('Missing CLEANCLOUD_API_TOKEN. Run setCleanCloudToken first.');
  if (requestState.used >= CONFIG.maxRequestsPerRun) throw new Error('CleanCloud request cap reached.');

  Utilities.sleep(CONFIG.throttleMs);

  const response = UrlFetchApp.fetch(`${CONFIG.cleanCloudApiBaseUrl}/${endpoint}`, {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(Object.assign({ api_token: token }, payload)),
    muteHttpExceptions: true,
  });

  requestState.used++;

  const status = response.getResponseCode();
  const text = response.getContentText();
  if (status < 200 || status >= 300) throw new Error(`${endpoint} HTTP ${status}: ${text.slice(0, 300)}`);

  const json = JSON.parse(text);
  if (json.Success && json.Success !== 'True') {
    throw new Error(`${endpoint}: ${json.Error || text.slice(0, 300)}`);
  }
  return json;
}

function buildOrderRow_(order, customer, statusMap, staffMap, paymentMap, nowText) {
  const receiptLink = fullReceiptLink_(order.receiptLink || '');
  const delivery = extractDeliveryFields_(order.summary);
  const row = [
    stringOrBlank_(order.id),
    unixToDateOrBlank_(order.createdDate),
    mapStaff_(nestedValue_(order, 'staffIds.create'), staffMap),
    unixToDateOrBlank_(order.cleanedDate),
    mapStaff_(nestedValue_(order, 'staffIds.cleaned'), staffMap),
    unixToDateOrBlank_(order.completedDate),
    mapStaff_(nestedValue_(order, 'staffIds.completed'), staffMap),
    stringOrBlank_(customer.Name || order.customer || ''),
    stringOrBlank_(order.customerID),
    stringOrBlank_(customer.Tel || order.phone || ''),
    stringOrBlank_(order.address || customer.Address || ''),
    stringOrBlank_(order.pieces),
    stringOrBlank_(order.summary),
    delivery.type,
    delivery.area,
    numberOrString_(order.total),
    mapStatus_(order.status, statusMap),
    receiptLink,
    nowText,
    'synced',
    '',
    stringOrBlank_(order.invoiceUniqueID),
    stringOrBlank_(order.invoiceStatus),
    mapPaid_(order.paid, paymentMap),
    mapPaymentType_(order.paymentType, paymentMap),
    unixToDateOrBlank_(order.paymentTime),
    numberOrString_(order.tax1),
    numberOrString_(order.tax2),
    numberOrString_(order.tax3),
  ];
  row[ORDER_HEADERS.indexOf('apiRawHash')] = md5_(JSON.stringify(order));
  return row;
}

function preserveExistingWhenBlank_(nextValues, currentValues, headerMap, fields) {
  fields.forEach(field => {
    const index = headerMap[field];
    if (index === undefined) return;
    if (isBlank_(nextValues[index]) && !isBlank_(currentValues[index])) {
      nextValues[index] = currentValues[index];
    }
  });
  return nextValues;
}

function haveFieldsChanged_(nextValues, currentValues, headerMap, fields) {
  return fields.some(field => {
    const index = headerMap[field];
    if (index === undefined) return false;
    return String(nextValues[index] || '').trim() !== String(currentValues[index] || '').trim();
  });
}

function isBlank_(value) {
  return value === '' || value === null || value === undefined;
}

function extractDeliveryFields_(summary) {
  const text = String(summary || '')
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/&amp;/g, '&');
  const lines = text.split('\n').map(line => line.replace(/\s+/g, ' ').trim()).filter(Boolean);
  const preferredMatch = lines.find(line => /^(Delivery\s*-|Postal\s+Courier\s+Charges|In\s*-?\s*store\b).*\(D\)(?:\s*x\s*\d+(?:\.\d+)?)?$/i.test(line));
  const fallbackMatch = lines.find(line => /\(D\)(?:\s*x\s*\d+(?:\.\d+)?)?$/i.test(line));
  const match = preferredMatch || fallbackMatch || '';
  const clean = match.replace(/\s*x\s*\d+(?:\.\d+)?$/i, '').trim();
  if (!clean) return { type: '', area: '' };

  const inStoreMatch = clean.match(/^In\s*-?\s*store\s*\(([^)]+)\)\s*\(D\)$/i);
  if (inStoreMatch) return { type: 'in store', area: inStoreMatch[1].trim() };

  const deliveryMatch = clean.match(/^Delivery\s*-\s*(.*?)\s*\(D\)$/i);
  if (deliveryMatch) return { type: 'delivery', area: deliveryMatch[1].trim() };

  if (/^Postal\s+Courier\s+Charges\s*\(D\)$/i.test(clean)) {
    return { type: 'courier', area: 'Courier' };
  }

  const fallbackArea = clean.replace(/\s*\(D\)$/i, '').trim();
  return { type: '', area: fallbackArea };
}

function isCustomerCacheStale_(customer, maxAgeDays) {
  if (!maxAgeDays && maxAgeDays !== 0) return false;
  const lastSeenAt = customer.lastSeenAt;
  if (!lastSeenAt) return true;
  const lastSeenDate = lastSeenAt instanceof Date ? lastSeenAt : new Date(String(lastSeenAt).replace(' ', 'T'));
  if (Number.isNaN(lastSeenDate.getTime())) return true;
  return (new Date().getTime() - lastSeenDate.getTime()) > maxAgeDays * 24 * 60 * 60 * 1000;
}

function loadStatusMap_(sheet) {
  const values = sheet.getDataRange().getValues();
  const header = values.shift();
  const codeIndex = header.indexOf('statusCode');
  const nameIndex = header.indexOf('statusName');
  const map = {};
  values.forEach(row => {
    const code = String(row[codeIndex] || '').trim();
    const name = String(row[nameIndex] || '').trim();
    if (code || name === 'unknown') map[code] = name || 'unknown';
  });
  return map;
}

function mapStatus_(status, statusMap) {
  const code = String(status === null || status === undefined ? '' : status).trim();
  return statusMap[code] || statusMap[''] || 'unknown';
}

function loadSimpleMap_(sheet, keyHeader, valueHeader) {
  const values = sheet.getDataRange().getValues();
  const header = values.shift();
  const keyIndex = header.indexOf(keyHeader);
  const valueIndex = header.indexOf(valueHeader);
  const map = {};
  values.forEach(row => {
    const key = String(row[keyIndex] || '').trim();
    const value = String(row[valueIndex] || '').trim();
    if (key && value) map[key] = value;
  });
  return map;
}

function loadPaymentMap_(sheet) {
  const values = sheet.getDataRange().getValues();
  const header = values.shift();
  const map = { paid: {}, paymentType: {} };
  const typeIndex = header.indexOf('field');
  const codeIndex = header.indexOf('code');
  const labelIndex = header.indexOf('label');
  values.forEach(row => {
    const field = String(row[typeIndex] || '').trim();
    const code = String(row[codeIndex] || '').trim();
    const label = String(row[labelIndex] || '').trim();
    if (field && label) map[field][code] = label;
  });
  return map;
}

function mapStaff_(staffId, staffMap) {
  const id = String(staffId || '').trim();
  if (!id || id === '0') return '';
  return staffMap[id] || id;
}

function mapPaid_(paid, paymentMap) {
  const code = String(paid === null || paid === undefined ? '' : paid).trim();
  return paymentMap.paid[code] || code || 'unknown';
}

function mapPaymentType_(paymentType, paymentMap) {
  const code = String(paymentType === null || paymentType === undefined ? '' : paymentType).trim();
  return paymentMap.paymentType[code] || code || 'unknown';
}

function buildApiCacheRow_(order, rowHash, nowText) {
  return [
    stringOrBlank_(order.id),
    stringOrBlank_(order.customerID),
    stringOrBlank_(order.invoiceUniqueID),
    fullReceiptLink_(order.receiptLink || ''),
    latestOrderTimestamp_(order),
    rowHash,
    nowText,
    'seen',
    'getOrders',
    '',
    'id',
    '',
  ];
}

function buildCustomerCacheRow_(customer, nowText) {
  return [
    stringOrBlank_(customer.ID),
    stringOrBlank_(customer.Name),
    stringOrBlank_(customer.Email),
    stringOrBlank_(customer.Tel),
    stringOrBlank_(customer.Address),
    nestedValue_(customer, 'addressDetailed.street'),
    nestedValue_(customer, 'addressDetailed.city'),
    stringOrBlank_(customer.customerAddressInstructions),
    stringOrBlank_(customer.Notes),
    unixToDateOrBlank_(customer.signedUpTimeStamp),
    stringOrBlank_(customer.marketingOptIn),
    stringOrBlank_(customer.businessID),
    stringOrBlank_(customer.taxID),
    md5_(JSON.stringify(customer)),
    nowText,
    'seen',
  ];
}

function appendToCache_(sheet, rows, width) {
  if (!rows.length) return;
  sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, width).setValues(rows);
}

function appendSyncLog_(sheet, entry) {
  sheet.appendRow([
    entry.runAt,
    entry.runType,
    entry.requestsUsed,
    entry.recordsPulled,
    entry.rowsUpdated,
    entry.rowsAppended,
    entry.receiptLinksFilled,
    entry.errors,
    entry.nextSince,
    entry.notes,
  ]);
}

function appendFailedSyncLog_(sheet, runType, requestState, startedAt, error) {
  appendSyncLog_(sheet, {
    runAt: formatDateTime_(new Date()),
    runType,
    requestsUsed: requestState.used,
    recordsPulled: 0,
    rowsUpdated: 0,
    rowsAppended: 0,
    receiptLinksFilled: 0,
    errors: requestState.errors.concat([error.message]).join(' | '),
    nextSince: '',
    notes: `failedAfterSec=${Math.round((new Date() - startedAt) / 1000)}`,
  });
}

function loadCustomerCache_(sheet) {
  const values = sheet.getDataRange().getValues();
  const header = values.shift();
  const idIndex = header.indexOf('ID');
  const cache = {};
  values.forEach(row => {
    const id = String(row[idIndex] || '').trim();
    if (!id) return;
    const customer = {};
    header.forEach((name, index) => {
      customer[name] = row[index];
    });
    cache[id] = customer;
  });
  return cache;
}

function getIdToRowMap_(sheet, idColumnIndexZeroBased) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return {};
  const ids = sheet.getRange(2, idColumnIndexZeroBased + 1, lastRow - 1, 1).getValues();
  const map = {};
  ids.forEach((row, index) => {
    const id = String(row[0] || '').trim();
    if (id) map[id] = index + 2;
  });
  return map;
}

function assertHeaders_(sheet, expectedHeaders) {
  const headers = sheet.getRange(1, 1, 1, expectedHeaders.length).getValues()[0];
  expectedHeaders.forEach((expected, index) => {
    if (headers[index] !== expected) {
      throw new Error(`${sheet.getName()} header mismatch at column ${index + 1}: expected "${expected}", got "${headers[index]}"`);
    }
  });
}

function getHeaderMap_(sheet) {
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const map = {};
  headers.forEach((header, index) => {
    if (header) map[header] = index;
  });
  return map;
}

function mustGetSheet_(ss, name) {
  const sheet = ss.getSheetByName(name);
  if (!sheet) throw new Error(`Missing sheet: ${name}`);
  return sheet;
}

function fullReceiptLink_(path) {
  const value = String(path || '').trim();
  if (!value) return '';
  if (/^https?:\/\//i.test(value)) return value;
  return `${CONFIG.cleanCloudBaseUrl}/${value.replace(/^\/+/, '')}`;
}

function latestOrderTimestamp_(order) {
  const candidates = [
    order.completedDate,
    order.cleanedDate,
    order.createdDate,
    order.paymentTime,
    order.deliveryDate,
  ].map(numberValue_).filter(value => value > 0);
  if (!candidates.length) return '';
  return unixToDateOrBlank_(Math.max.apply(null, candidates));
}

function unixToDateOrBlank_(value) {
  const number = numberValue_(value);
  if (!number) return '';
  return new Date(number * 1000);
}

function formatDateTime_(date) {
  return Utilities.formatDate(date, CONFIG.timezone, 'yyyy-MM-dd HH:mm:ss');
}

function nestedValue_(obj, path) {
  return path.split('.').reduce((current, key) => current && current[key] !== undefined ? current[key] : '', obj) || '';
}

function numberValue_(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

function numberOrString_(value) {
  if (value === null || value === undefined || value === '') return '';
  const number = Number(value);
  return Number.isFinite(number) ? number : String(value);
}

function stringOrBlank_(value) {
  if (value === null || value === undefined) return '';
  return String(value);
}

function normalizeArray_(value) {
  if (!value) return [];
  return Array.isArray(value) ? value : [value];
}

function unique_(values) {
  return Array.from(new Set(values));
}

function md5_(input) {
  const bytes = Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, input);
  return bytes.map(byte => {
    const value = (byte < 0 ? byte + 256 : byte).toString(16);
    return value.length === 1 ? `0${value}` : value;
  }).join('');
}
