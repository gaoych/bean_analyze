const state = {
  roots: [],
  currentRoot: null,
  graphData: null,
  svg: null,
  zoom: null,
  simulation: null,
  nodeSelection: null,
  linkSelection: null,
  labelSelection: null,
  nodeById: new Map(),
  highlightedNode: null,
  unusedChains: [],
  chainSummary: null,
  excludeSpring: false,
  excludeThirdParty: false,
  availableThirdPartyPackages: [],
  selectedThirdPartyPackages: [],
  searchTerm: '',
  searchMatches: [],
  searchIndex: -1,
};

const selectors = {
  rootSelect: () => document.getElementById('root-select'),
  loadRootButton: () => document.getElementById('load-root'),
  loadAllButton: () => document.getElementById('load-all'),
  searchInput: () => document.getElementById('search-input'),
  resetViewButton: () => document.getElementById('reset-view'),
  searchPrevButton: () => document.getElementById('search-prev'),
  searchNextButton: () => document.getElementById('search-next'),
  excludeSpringCheckbox: () => document.getElementById('exclude-spring'),
  excludeThirdPartyCheckbox: () => document.getElementById('exclude-third-party'),
  thirdPartyPackageContainer: () => document.getElementById('third-party-package-container'),
  thirdPartyPackageSelect: () => document.getElementById('third-party-packages'),
  statusMessage: () => document.getElementById('status-message'),
  placeholder: () => document.getElementById('graph-placeholder'),
  statNodes: () => document.getElementById('stat-nodes'),
  statEdges: () => document.getElementById('stat-edges'),
  statRoots: () => document.getElementById('stat-roots'),
  statUnused: () => document.getElementById('stat-unused'),
  details: () => document.getElementById('details'),
  unusedList: () => document.getElementById('unused-chains-list'),
  chainSummary: () => document.getElementById('chain-summary'),
};

const API_BASE = determineApiBase();
const RUNNING_FROM_FILE = window.location.protocol === 'file:';

document.addEventListener('DOMContentLoaded', () => {
  setupUI();
  loadRoots();
});

function determineApiBase() {
  const globalConfigBase = window.APP_CONFIG?.apiBase;
  if (globalConfigBase) {
    return globalConfigBase.replace(/\/+$/, '');
  }

  const params = new URLSearchParams(window.location.search);
  const explicitBase = params.get('apiBase');
  if (explicitBase) {
    return explicitBase.replace(/\/+$/, '');
  }

  const portParam = params.get('port');
  if (portParam) {
    return `http://localhost:${portParam}`;
  }

  if (window.location.origin && window.location.origin !== 'null') {
    return '';
  }

  return 'http://localhost:8000';
}

function buildApiUrl(path) {
  if (!path.startsWith('/')) {
    throw new Error('API path must start with a leading slash.');
  }

  if (!API_BASE) {
    return path;
  }

  return `${API_BASE}${path}`;
}

function buildGraphDataUrl(root) {
  const params = new URLSearchParams();
  params.set('root', root ? root : 'all');
  if (state.excludeSpring) {
    params.set('excludeSpring', 'true');
  }
  if (state.excludeThirdParty) {
    params.set('excludeThirdParty', 'true');
    if (state.selectedThirdPartyPackages.length) {
      params.set('thirdPartyPackages', state.selectedThirdPartyPackages.join(','));
    }
  }
  return buildApiUrl(`/graph-data?${params.toString()}`);
}

function setupUI() {
  selectors.loadRootButton().addEventListener('click', () => {
    const select = selectors.rootSelect();
    const root = select.value;
    if (!root) {
      setStatus('请选择一个最外层端点。', 'warn');
      return;
    }
    loadGraph(root);
  });

  selectors.loadAllButton().addEventListener('click', () => {
    loadGraph(null);
  });

  selectors.searchInput().addEventListener('input', handleSearchInput);
  const prevButton = selectors.searchPrevButton();
  if (prevButton) {
    prevButton.addEventListener('click', () => stepSearch(-1));
  }
  const nextButton = selectors.searchNextButton();
  if (nextButton) {
    nextButton.addEventListener('click', () => stepSearch(1));
  }

  const excludeCheckbox = selectors.excludeSpringCheckbox();
  if (excludeCheckbox) {
    excludeCheckbox.addEventListener('change', async (event) => {
      state.excludeSpring = event.target.checked;
      await reloadDataAfterFilterChange({
        messageWhenCleared: state.excludeSpring
          ? '当前起点属于 Spring Bean，已被过滤。请选择其他起点。'
          : '已重新包含 Spring Bean，请选择需要查看的起点。',
        statusLevel: state.excludeSpring ? 'warn' : 'info',
      });
    });
  }

  const thirdPartySelect = selectors.thirdPartyPackageSelect();
  if (thirdPartySelect) {
    thirdPartySelect.disabled = true;
    thirdPartySelect.addEventListener('change', async () => {
      const selected = Array.from(thirdPartySelect.selectedOptions).map((option) => option.value);
      state.selectedThirdPartyPackages = selected;
      if (!state.excludeThirdParty) {
        return;
      }
      await reloadDataAfterFilterChange({
        messageWhenCleared: '当前起点属于被过滤的三方包，请选择其他起点。',
        statusLevel: 'warn',
      });
    });
  }

  const thirdPartyCheckbox = selectors.excludeThirdPartyCheckbox();
  if (thirdPartyCheckbox) {
    thirdPartyCheckbox.addEventListener('change', async (event) => {
      state.excludeThirdParty = event.target.checked;
      if (state.excludeThirdParty && !state.selectedThirdPartyPackages.length) {
        state.selectedThirdPartyPackages = getAllThirdPartyPackageIds();
      }
      renderThirdPartyPackageOptions();
      await reloadDataAfterFilterChange({
        messageWhenCleared: state.excludeThirdParty
          ? '当前起点属于被过滤的三方包，请选择其他起点。'
          : '已重新包含三方包 Bean，请选择需要查看的起点。',
        statusLevel: state.excludeThirdParty ? 'warn' : 'info',
      });
    });
  }

  selectors.resetViewButton().addEventListener('click', resetView);
}

async function loadRoots(options = {}) {
  const { preserveSelection = false } = options;
  const select = selectors.rootSelect();
  const previousValue = preserveSelection && select ? select.value : '';
  setStatus('正在加载起点列表…', 'info');
  try {
    const params = new URLSearchParams();
    if (state.excludeSpring) {
      params.set('excludeSpring', 'true');
    }
    if (state.excludeThirdParty) {
      params.set('excludeThirdParty', 'true');
      if (state.selectedThirdPartyPackages.length) {
        params.set('thirdPartyPackages', state.selectedThirdPartyPackages.join(','));
      }
    }
    const url = params.toString() ? `/roots?${params.toString()}` : '/roots';
    const response = await fetch(buildApiUrl(url));
    if (!response.ok) {
      throw new Error(`获取起点失败：${response.status}`);
    }
    const data = await response.json();
    state.roots = data.roots || [];
    state.unusedChains = data.unusedChains || [];
    state.availableThirdPartyPackages = data.thirdPartyPackages || [];
    renderThirdPartyPackageOptions();
    populateRootSelect(state.roots);
    if (select && preserveSelection && previousValue && state.roots.includes(previousValue)) {
      select.value = previousValue;
    }
    renderUnusedChains();
    const unusedCountEl = selectors.statUnused();
    if (unusedCountEl) {
      unusedCountEl.textContent = state.unusedChains.length.toLocaleString('zh-CN');
    }
    const statRootsEl = selectors.statRoots();
    if (statRootsEl) {
      statRootsEl.textContent = state.roots.length.toLocaleString('zh-CN');
    }
    updateChainSummary();
    updateSearchNavButtons();
    if (state.roots.length) {
      setStatus('请选择一个最外层端点并点击“加载链路”。', 'info');
    } else {
      setStatus('未在数据中找到起点。', 'warn');
    }
    return true;
  } catch (error) {
    console.error(error);
    const rawBase = API_BASE || window.location.origin || 'http://localhost:8000';
    const displayBase = !rawBase || rawBase === 'null' ? 'http://localhost:8000' : rawBase;
    if (RUNNING_FROM_FILE) {
      setStatus(
        `加载起点列表失败。当前页面通过 file:// 打开，浏览器会阻止访问接口。请运行后端服务后在浏览器访问 ${displayBase}，` +
          '如需指定端口，可在地址后加上 ?port=端口号。',
        'error',
      );
    } else {
      setStatus(`加载起点列表失败，请检查后端服务（${displayBase}）。`, 'error');
    }
    state.roots = [];
    state.unusedChains = [];
    state.availableThirdPartyPackages = [];
    renderThirdPartyPackageOptions();
    updateSearchNavButtons();
    resetStatsDisplay();
    return false;
  }
}

function populateRootSelect(roots) {
  const select = selectors.rootSelect();
  select.innerHTML = '';
  if (!roots.length) {
    const option = document.createElement('option');
    option.textContent = '无可用起点';
    option.disabled = true;
    select.appendChild(option);
    return;
  }

  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = '请选择一个起点';
  placeholder.disabled = true;
  placeholder.selected = true;
  select.appendChild(placeholder);

  for (const root of roots) {
    const option = document.createElement('option');
    option.value = root;
    option.textContent = root;
    select.appendChild(option);
  }
}

function getAllThirdPartyPackageIds() {
  const packages = state.availableThirdPartyPackages || [];
  return packages.map((pkg) => pkg.package || pkg.name || pkg.id).filter(Boolean);
}

function renderThirdPartyPackageOptions() {
  const container = selectors.thirdPartyPackageContainer();
  const select = selectors.thirdPartyPackageSelect();
  if (!container || !select) {
    return;
  }

  const packages = state.availableThirdPartyPackages || [];
  container.hidden = !packages.length;

  const availableValues = packages
    .map((pkg) => pkg.package || pkg.name || pkg.id)
    .filter(Boolean);

  if (state.selectedThirdPartyPackages.length) {
    state.selectedThirdPartyPackages = state.selectedThirdPartyPackages.filter((value) =>
      availableValues.includes(value),
    );
  }

  select.innerHTML = '';

  if (!packages.length) {
    select.disabled = true;
    return;
  }

  select.disabled = false;
  select.size = Math.min(8, Math.max(4, packages.length));

  const selectionSet = new Set(state.selectedThirdPartyPackages);

  packages.forEach((pkg) => {
    const value = pkg.package || pkg.name || pkg.id;
    if (!value) {
      return;
    }
    const option = document.createElement('option');
    option.value = value;
    const count = typeof pkg.beanCount === 'number' ? pkg.beanCount : undefined;
    option.textContent =
      count !== undefined
        ? `${value}（${count.toLocaleString('zh-CN')} 个 Bean）`
        : value;
    option.selected = selectionSet.has(value);
    select.appendChild(option);
  });
}

async function reloadDataAfterFilterChange(options = {}) {
  const { messageWhenCleared, statusLevel = 'warn' } = options;
  const previousRoot = state.currentRoot;
  const hadFullGraph = !previousRoot && Boolean(state.graphData);

  const loaded = await loadRoots({ preserveSelection: true });
  if (!loaded) {
    return;
  }

  const select = selectors.rootSelect();
  if (previousRoot && state.roots.includes(previousRoot)) {
    if (select) {
      select.value = previousRoot;
    }
    await loadGraph(previousRoot);
    return;
  }

  if (!previousRoot && hadFullGraph) {
    await loadGraph(null);
    return;
  }

  state.currentRoot = null;
  state.graphData = null;
  if (select) {
    select.value = '';
  }
  clearGraphView('当前过滤条件下未加载任何链路，请选择起点。');
  state.chainSummary = null;
  updateChainSummary();
  resetStatsDisplay();
  renderUnusedChains();
  if (messageWhenCleared) {
    setStatus(messageWhenCleared, statusLevel);
  }
}

async function loadGraph(root) {
  const rootLabel = root ? `起点 ${root}` : '全部节点';
  setStatus(`正在加载 ${rootLabel} 的数据…`, 'info');
  state.searchMatches = [];
  state.searchIndex = -1;
  updateSearchNavButtons();

  try {
    const url = buildGraphDataUrl(root);
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`服务返回错误：${response.status}`);
    }
    const data = await response.json();
    state.graphData = data;
    state.currentRoot = root || null;
    state.chainSummary = data.chainSummary || null;
    if (Array.isArray(data.thirdPartyPackages)) {
      state.availableThirdPartyPackages = data.thirdPartyPackages;
      renderThirdPartyPackageOptions();
    }
    buildGraph(data);
    updateStats(data);
    updateChainSummary();
    renderUnusedChains();
    const messageParts = [`已加载 ${rootLabel}，共 ${data.nodes.length} 个节点。`];
    if (data.isUnusedChain) {
      messageParts.push('该链路未被其他链路引用。');
    } else if (root && data.chainSummary) {
      const summary = data.chainSummary;
      if (summary.externalReferencerCount) {
        messageParts.push(
          `有 ${summary.externallyReferencedNodes.toLocaleString('zh-CN')} 个节点被其他链路引用（${summary.externalReferencerCount.toLocaleString('zh-CN')} 个引用来源）。`,
        );
      }
    } else if (!root && state.unusedChains.length) {
      messageParts.push(
        `当前共有 ${state.unusedChains.length.toLocaleString('zh-CN')} 个起点的链路未被其他链路引用，可在右侧列表中查看。`,
      );
    }
    setStatus(messageParts.join(' '), 'success');
    if (state.searchTerm) {
      applySearchTerm({ resetIndex: true, centerOnMatch: false });
    } else {
      updateSearchNavButtons();
    }
  } catch (error) {
    console.error(error);
    setStatus('加载链路数据失败，请查看控制台日志。', 'error');
  }
}

function clearGraphView(message) {
  if (state.simulation) {
    state.simulation.stop();
    state.simulation = null;
  }

  const svg = d3.select('#graph');
  svg.selectAll('*').remove();
  state.svg = svg;
  state.zoom = null;

  const placeholder = selectors.placeholder();
  if (placeholder) {
    placeholder.textContent = message || '请选择一个起点并点击“加载链路”。';
    placeholder.style.display = 'grid';
  }

  state.nodeSelection = null;
  state.linkSelection = null;
  state.labelSelection = null;
  state.nodeById = new Map();
  state.highlightedNode = null;
  state.searchMatches = [];
  state.searchIndex = -1;
  updateSearchNavButtons();
  highlightNode(null);
  showDetails();
}

function buildGraph(data) {
  const container = document.getElementById('graph-container');
  const svg = d3.select('#graph');
  svg.selectAll('*').remove();

  const width = container.clientWidth;
  const height = container.clientHeight;
  svg.attr('viewBox', [0, 0, width, height]);

  if (!data.nodes.length) {
    selectors.placeholder().textContent = '当前选择没有任何节点。';
    selectors.placeholder().style.display = 'grid';
    state.nodeSelection = null;
    state.linkSelection = null;
    state.labelSelection = null;
    state.nodeById = new Map();
    showDetails();
    return;
  }

  selectors.placeholder().style.display = 'none';

  if (state.simulation) {
    state.simulation.stop();
  }

  const defs = svg.append('defs');
  defs
    .append('marker')
    .attr('id', 'arrowhead')
    .attr('viewBox', '0 -5 10 10')
    .attr('refX', 12)
    .attr('refY', 0)
    .attr('markerWidth', 6)
    .attr('markerHeight', 6)
    .attr('orient', 'auto')
    .append('path')
    .attr('d', 'M0,-5L10,0L0,5')
    .attr('fill', '#9ca3af');

  const g = svg.append('g');

  const zoom = d3
    .zoom()
    .scaleExtent([0.1, 8])
    .on('zoom', (event) => {
      g.attr('transform', event.transform);
    });
  svg.call(zoom);
  state.zoom = zoom;

  const link = g
    .append('g')
    .attr('stroke', '#cbd5f5')
    .attr('stroke-width', 1.1)
    .selectAll('line')
    .data(data.edges)
    .join('line')
    .attr('marker-end', 'url(#arrowhead)');

  const nodeGroup = g.append('g');

  const node = nodeGroup
    .selectAll('g')
    .data(data.nodes, (d) => d.id)
    .join('g')
    .call(
      d3
        .drag()
        .on('start', (event, d) => {
          if (!event.active) state.simulation.alphaTarget(0.3).restart();
          d.fx = d.x;
          d.fy = d.y;
        })
        .on('drag', (event, d) => {
          d.fx = event.x;
          d.fy = event.y;
        })
        .on('end', (event, d) => {
          if (!event.active) state.simulation.alphaTarget(0);
          d.fx = null;
          d.fy = null;
        })
    );

  node
    .append('circle')
    .attr('r', 8)
    .attr('fill', nodeColor)
    .attr('stroke', (d) => (d.isRoot && d.hasDependencies ? '#1b5e20' : '#1f2933'))
    .attr('stroke-width', (d) => (d.isRoot && d.hasDependencies ? 2 : 1.2));

  node
    .append('title')
    .text((d) => `${d.label}\n依赖数量：${d.dependencies.length}\n被依赖次数：${d.dependentCount}`);

  node
    .append('text')
    .text((d) => d.label)
    .attr('x', 12)
    .attr('y', 4)
    .attr('font-size', 11)
    .attr('fill', '#1f2937')
    .attr('pointer-events', 'none');

  node.on('click', (_, d) => {
    highlightNode(d.id);
    showDetails(d);
  });

  state.simulation = d3
    .forceSimulation(data.nodes)
    .force(
      'link',
      d3
        .forceLink(data.edges)
        .id((d) => d.id)
        .distance(90)
        .strength(0.25)
    )
    .force('charge', d3.forceManyBody().strength(-220))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collision', d3.forceCollide().radius(28));

  state.simulation.on('tick', () => {
    link
      .attr('x1', (d) => d.source.x)
      .attr('y1', (d) => d.source.y)
      .attr('x2', (d) => d.target.x)
      .attr('y2', (d) => d.target.y);

    node.attr('transform', (d) => `translate(${d.x},${d.y})`);
  });

  state.svg = svg;
  state.nodeSelection = node;
  state.linkSelection = link;
  state.labelSelection = node.select('text');
  state.nodeById = new Map(data.nodes.map((n) => [n.id, n]));
  state.highlightedNode = null;
  highlightNode(null);
  showDetails();
}

function nodeColor(node) {
  if (!node.hasDependencies) {
    return '#e53935';
  }
  if (node.missing) {
    return '#6b7280';
  }
  if (node.isRoot) {
    return '#2e7d32';
  }
  return '#1d4ed8';
}

function updateStats(data) {
  selectors.statNodes().textContent = data.nodes.length.toLocaleString('zh-CN');
  selectors.statEdges().textContent = data.edges.length.toLocaleString('zh-CN');
  selectors.statRoots().textContent = (data.roots || []).length.toLocaleString('zh-CN');
  const unusedCountEl = selectors.statUnused();
  if (unusedCountEl) {
    unusedCountEl.textContent = state.unusedChains.length.toLocaleString('zh-CN');
  }
}

function resetStatsDisplay() {
  selectors.statNodes().textContent = '0';
  selectors.statEdges().textContent = '0';
  selectors.statRoots().textContent = state.roots.length.toLocaleString('zh-CN');
  const unusedCountEl = selectors.statUnused();
  if (unusedCountEl) {
    unusedCountEl.textContent = state.unusedChains.length.toLocaleString('zh-CN');
  }
}

function renderUnusedChains() {
  const container = selectors.unusedList();
  if (!container) {
    return;
  }

  const chains = state.unusedChains || [];
  if (!chains.length) {
    container.innerHTML = '<p class="empty">暂未发现完全未被引用的链路。</p>';
    return;
  }

  const list = document.createElement('ul');
  list.className = 'unused-list';

  chains.forEach((info) => {
    const item = document.createElement('li');
    if (state.currentRoot === info.root) {
      item.classList.add('active');
    }

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'link';
    button.textContent = info.root;
    button.addEventListener('click', () => {
      const select = selectors.rootSelect();
      if (select) {
        select.value = info.root;
      }
      loadGraph(info.root);
    });

    const meta = document.createElement('span');
    meta.className = 'meta';
    const nodeCountLabel = Number(info.nodeCount || 0).toLocaleString('zh-CN');
    const leafCountLabel = Number(info.leafCount || 0).toLocaleString('zh-CN');
    meta.textContent = `节点 ${nodeCountLabel} · 终点 ${leafCountLabel}`;

    item.append(button, meta);
    list.appendChild(item);
  });

  container.innerHTML = '';
  container.appendChild(list);
}

function updateChainSummary() {
  const panel = selectors.chainSummary();
  if (!panel) {
    return;
  }

  const summary = state.chainSummary;
  if (!summary) {
    panel.innerHTML = '<h2>链路信息</h2><p>加载起点后将显示链路概览。</p>';
    return;
  }

  if (!summary.root) {
    panel.innerHTML = `
      <h2>链路信息</h2>
      <p>当前查看全部 ${summary.nodeCount.toLocaleString('zh-CN')} 个节点。</p>
      <p>链路终点共 ${summary.leafCount.toLocaleString('zh-CN')} 个。</p>
      <p>未被其他链路使用的起点数量：${state.unusedChains.length.toLocaleString('zh-CN')}。</p>
    `;
    return;
  }

  const statusClass = summary.isUnused ? 'unused' : 'used';
  let statusText = '';
  if (summary.isUnused) {
    statusText = '该链路未被其他链路引用，可重点排查是否仍需保留。';
  } else if (summary.externalReferencerCount) {
    statusText = `有 ${summary.externallyReferencedNodes.toLocaleString('zh-CN')} 个节点被其他链路引用（${summary.externalReferencerCount.toLocaleString('zh-CN')} 个引用来源）。`;
  } else {
    statusText = '该链路存在外部引用。';
  }

  panel.innerHTML = `
    <h2>链路信息</h2>
    <p><strong>当前起点：</strong>${summary.root}</p>
    <p><strong>节点数：</strong>${summary.nodeCount.toLocaleString('zh-CN')}</p>
    <p><strong>链路终点：</strong>${summary.leafCount.toLocaleString('zh-CN')}</p>
    <p class="chain-status ${statusClass}">${statusText}</p>
  `;
}

function handleSearchInput() {
  const input = selectors.searchInput();
  if (!input) {
    return;
  }
  state.searchTerm = input.value.trim();

  if (!state.nodeSelection) {
    state.searchMatches = [];
    state.searchIndex = -1;
    updateSearchNavButtons();
    if (state.searchTerm) {
      setStatus('请先加载链路数据。', 'warn');
    } else {
      setStatus('输入关键字以在当前视图中查找 Bean。', 'info');
    }
    return;
  }

  applySearchTerm({ resetIndex: true, centerOnMatch: true });
}

function applySearchTerm(options = {}) {
  const { resetIndex = false, centerOnMatch = false } = options;

  if (!state.nodeSelection) {
    state.searchMatches = [];
    state.searchIndex = -1;
    updateSearchNavButtons();
    return;
  }

  const term = state.searchTerm.toLowerCase();
  const hasTerm = Boolean(term);
  const matches = [];

  state.nodeSelection.select('circle').classed('matched', (d) => {
    const match = hasTerm && d.id.toLowerCase().includes(term);
    if (match) {
      matches.push(d.id);
    }
    return match;
  });

  state.nodeSelection
    .select('text')
    .classed('matched', (d) => hasTerm && d.id.toLowerCase().includes(term));

  state.searchMatches = matches;
  if (resetIndex) {
    state.searchIndex = matches.length ? 0 : -1;
  } else if (state.searchIndex >= matches.length) {
    state.searchIndex = matches.length ? matches.length - 1 : -1;
  }

  updateSearchNavButtons();

  if (hasTerm && matches.length) {
    if (state.searchIndex < 0) {
      state.searchIndex = 0;
    }
    const nodeId = state.searchMatches[state.searchIndex];
    highlightNode(nodeId, centerOnMatch);
    const node = state.nodeById.get(nodeId);
    if (node) {
      showDetails(node);
    }
    setStatus(`找到匹配项 (${state.searchIndex + 1}/${matches.length})：${nodeId}`, 'success');
  } else if (hasTerm) {
    highlightNode(null);
    setStatus('未找到匹配的 Bean。', 'warn');
  } else {
    highlightNode(null);
    setStatus('输入关键字以在当前视图中查找 Bean。', 'info');
  }
}

function stepSearch(offset) {
  if (!state.nodeSelection || !state.searchMatches.length) {
    if (state.searchTerm) {
      setStatus('未找到匹配的 Bean。', 'warn');
    } else {
      setStatus('请输入搜索关键字后再跳转。', 'info');
    }
    return;
  }

  const total = state.searchMatches.length;
  if (state.searchIndex < 0) {
    state.searchIndex = 0;
  }
  state.searchIndex = (state.searchIndex + offset + total) % total;
  const nodeId = state.searchMatches[state.searchIndex];
  highlightNode(nodeId, true);
  const node = state.nodeById.get(nodeId);
  if (node) {
    showDetails(node);
  }
  setStatus(`查看匹配项 (${state.searchIndex + 1}/${total})：${nodeId}`, 'success');
}

function updateSearchNavButtons() {
  const hasMatches = Boolean(state.nodeSelection) && state.searchMatches.length > 0;
  const prev = selectors.searchPrevButton();
  const next = selectors.searchNextButton();
  if (prev) {
    prev.disabled = !hasMatches;
  }
  if (next) {
    next.disabled = !hasMatches;
  }
}

function highlightNode(nodeId, center = false) {
  if (!state.nodeSelection) {
    return;
  }

  state.nodeSelection.classed('highlighted', (d) => d.id === nodeId);
  state.nodeSelection.select('circle')
    .attr('stroke-width', (d) => {
      if (d.id === nodeId) {
        return 3;
      }
      return d.isRoot && d.hasDependencies ? 2 : 1.2;
    })
    .attr('stroke', (d) => {
      if (d.id === nodeId) {
        return '#f59e0b';
      }
      return d.isRoot && d.hasDependencies ? '#1b5e20' : '#1f2933';
    });

  state.highlightedNode = nodeId;

  if (center && nodeId && state.zoom && state.svg) {
    const selected = state.nodeSelection.filter((d) => d.id === nodeId);
    if (!selected.empty()) {
      const datum = selected.datum();
      const container = document.getElementById('graph-container');
      const width = container.clientWidth;
      const height = container.clientHeight;
      const transform = d3.zoomIdentity
        .translate(width / 2 - datum.x * 1.5, height / 2 - datum.y * 1.5)
        .scale(1.5);
      state.svg.transition().duration(500).call(state.zoom.transform, transform);
    }
  }
}

function showDetails(node) {
  const details = selectors.details();
  if (!node) {
    details.innerHTML = '<h2>节点详情</h2><p>点击图中的节点查看详细信息。</p>';
    return;
  }

  const meta = node.metadata || {};
  const dependencies = node.dependencies || [];
  const dependents = node.dependents || [];

  details.innerHTML = `
    <h2>节点详情</h2>
    <p><strong>名称：</strong>${node.id}</p>
    <p><strong>类型：</strong>${meta.type || '未知'}</p>
    <p><strong>Scope：</strong>${meta.scope || '未知'}</p>
    <p><strong>来源：</strong>${meta.source || '未知'}</p>
    <p><strong>是否三方包：</strong>${
      meta.isThirdPartyBean ? `是（${meta.thirdPartyPackage || '未识别包'}）` : '否'
    }</p>
    <p><strong>定义位置：</strong>${meta.definitionSource || '未知'}</p>
    <p><strong>分类：</strong>${(meta.categories || []).join(', ') || '无'}</p>
    <p><strong>是否附加 Bean：</strong>${meta.isAdditionalBean ? '是' : '否'}</p>
    <p><strong>直接依赖 (${dependencies.length})：</strong></p>
    ${renderRelationList(dependencies, 'dependency')}
    <p><strong>被依赖 (${dependents.length})：</strong></p>
    ${renderRelationList(dependents, 'dependent')}
  `;
}

function renderRelationList(items, type) {
  if (!items || !items.length) {
    return '<p class="empty">无</p>';
  }

  const lis = items
    .map((item) => {
      const known = state.nodeById.has(item);
      const button = known
        ? `<button class="link" data-type="${type}" data-target="${item}">${item}</button>`
        : `<span>${item}</span> (未在当前视图中)`;
      return `<li>${button}</li>`;
    })
    .join('');

  const listHtml = `<ul>${lis}</ul>`;

  setTimeout(() => {
    const detailPane = selectors.details();
    detailPane.querySelectorAll('button.link').forEach((btn) => {
      btn.addEventListener('click', () => {
        const target = btn.getAttribute('data-target');
        if (state.nodeById.has(target)) {
          highlightNode(target, true);
          showDetails(state.nodeById.get(target));
        } else {
          setStatus('该节点不在当前视图中，请加载相关起点。', 'warn');
        }
      });
    });
  }, 0);

  return listHtml;
}

function resetView() {
  selectors.searchInput().value = '';
  state.searchTerm = '';
  state.searchMatches = [];
  state.searchIndex = -1;
  if (state.nodeSelection) {
    state.nodeSelection.select('circle').classed('matched', false);
    state.nodeSelection.select('text').classed('matched', false);
  }
  updateSearchNavButtons();
  highlightNode(null);
  setStatus('视图已重置。', 'info');
  if (state.svg && state.zoom) {
    state.svg.transition().duration(500).call(state.zoom.transform, d3.zoomIdentity);
  }
}

function setStatus(message, level = 'info') {
  const el = selectors.statusMessage();
  el.textContent = message;
  el.dataset.level = level;
}

// Apply basic styling classes based on status level.
const statusStyle = document.createElement('style');
statusStyle.textContent = `
  .status[data-level='info'] { color: #2563eb; }
  .status[data-level='success'] { color: #16a34a; }
  .status[data-level='warn'] { color: #d97706; }
  .status[data-level='error'] { color: #dc2626; }
  #graph text.matched { font-weight: 700; fill: #b91c1c; }
  #graph circle.matched { stroke: #b91c1c !important; stroke-width: 2 !important; }
`;
document.head.appendChild(statusStyle);
