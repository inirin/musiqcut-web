# Frontend Style Guide

프론트엔드 UI 작업 시 반드시 따를 디자인 시스템.

## Spacing Scale (간격)

| 용도 | 값 | 적용 |
|------|-----|------|
| 최소 간격 | `4px` | label margin-bottom, 인접 텍스트 |
| 블록 간 간격 | `8px` | step-content-block, feedback, 서브요소 gap |
| 섹션 간 간격 | `12px` | card-title margin, setting-header, guide-flow |
| 카드 내부 패딩 | `20px` (모바일 `16px`) | .card |
| 서브요소 패딩 | `8px 10px` | 모든 내부 박스/입력 |

## Typography

| 용도 | CSS 변수 | 값 |
|------|---------|-----|
| 라벨/캡션 | `--text-xs` | 0.72rem |
| 본문/설명 | `--text-sm` | 0.82rem |
| 기본 텍스트 | `--text-base` | 0.9rem |
| 제목 | `--text-lg` | 1.05rem |
| 큰 제목 | `--text-xl` | 1.3rem |

- line-height: 본문 `1.6`, 컴팩트 `1.5`
- font-weight: 라벨/뱃지 `600`, 제목 `600`, 본문 `400`

## Border

| 용도 | 스타일 |
|------|--------|
| 기본 보더 | `1px solid var(--border)` |
| 강조 보더 | `border-left: 3px solid var(--accent/error/success)` |
| 구분선 | `border-bottom: 1px solid var(--border)` (리스트) |

- **background 대신 border 사용** (서브요소 분리 시)
- background는 카드(.card) 레벨에서만 사용

## Border Radius

| 용도 | CSS 변수 | 값 |
|------|---------|-----|
| 카드/스텝 | `--radius` | 12px |
| 서브요소/입력 | `--radius-sm` | 6px |
| 뱃지/태그 | `--radius-pill` | 20px |

## Colors

| 용도 | CSS 변수 |
|------|---------|
| 배경 | `--bg`, `--surface`, `--surface2` |
| 텍스트 | `--text` (기본), `--text-muted` (보조) |
| 보더 | `--border` |
| 강조 | `--accent` (보라), `--accent2` (연보라) |
| 상태 | `--success` (초록), `--warning` (노랑), `--error` (빨강) |

## Gap 규칙

| 컨텍스트 | 값 |
|----------|-----|
| flex 자식 간 | `8px` |
| grid 셀 간 | `8px` |
| 카드 그리드 | `16px` |
| 연결된 카드 (guide-flow) | `0` |

## 컴포넌트 패턴

### 서브 박스 (guide-io, step-prompt, gen-hist-item 등)
```css
padding: 8px 10px;
border: 1px solid var(--border);
border-radius: var(--radius-sm);
font-size: var(--text-sm);
color: var(--text-muted);
```

### 강조 섹션 (vocalist, guide-detail, failure)
```css
/* 위 서브 박스 + */
border-left: 3px solid var(--accent);
```

### 접기/펼치기 텍스트
```css
max-height: 1.5em;
overflow: hidden;
cursor: pointer;
text-overflow: ellipsis;
white-space: nowrap;
transition: max-height 0.2s, white-space 0.2s;
/* .open 시 */
max-height: 200px;
white-space: normal;
```

### 뱃지/태그
```css
font-size: var(--text-xs);
font-weight: 600;
padding: 1px 6px;
border-radius: var(--radius-sm);
/* 또는 pill: */
padding: 2px 8px;
border-radius: var(--radius-pill);
```

### 상태 dot
```css
width: 8px;
height: 8px;
border-radius: 50%;
/* 색상별 glow: */
box-shadow: 0 0 6px var(--success/error/accent);
/* 애니메이션 (running): */
animation: gen-pulse 1.5s ease-in-out infinite;
```

## 금지 사항

- `background: var(--surface2)` 로 서브요소 분리 → `border` 사용
- 하드코딩 px 대신 CSS 변수 사용 (radius, font-size)
- `margin-top`과 `margin-bottom` 혼용 → 한 방향만 (`margin-top` 또는 `margin-bottom`)
- 같은 레벨 요소에 다른 간격 → 통일된 클래스 사용 (`step-content-block`)
