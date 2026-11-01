# OCR → 번역 파이프라인 개선 마일스톤

## Context

`말풍선 번역 레이어`(v0.5.0)는 이미 증분 밴드 캡처, 프레임 서명 스킵, 문서/요소 앵커, 말풍선 완전성 판정, 유료 폴백 계량까지 갖춘 상태다. 그럼에도 실사용에서 네 가지가 걸린다.

1. **번역이 통째로 사라지거나 두 겹으로 쌓인다** — 중복 제거 로직의 경계 조건 결함.
2. **OCR이 느리다** — 화면에 안 나올 글자까지 인식하고, CJK 경로는 고해상도 + 라인별 방향 분류를 그대로 돈다.
3. **스크롤하면 패치가 밀리거나 사라진다** — canvas 뷰어에 앵커가 없고, 스크롤마다 JS가 패치를 다시 놓는다.
4. **말풍선 판정이 조용히 틀린다** — 축 방향 라인 스캔이 큰 말풍선과 그라데이션 배경에서 무너진다.

이 문서는 그 네 가지를 **버그 → 체감 속도 → 정확도** 순으로 정리한 실행 계획이다. OCR 속도는 모델·런타임 교체 없이 안전한 최적화 범위로 한정한다(ONNXRuntime 직접 실행, GPU 경로는 보류 항목).

아래 “확인됨” 표시가 붙은 항목은 이번 조사에서 실제로 실행해 재현한 것이고, 나머지는 코드 독해에 근거한 것이다.

## v0.6.0 적용 현황

- **완료**: M0의 결정적 벤치마크·CSV 출력, M1 전체(동률·혼동 문자·캡처 간 중복·429 재시도), M2-3(CJK 검출 상한)·M2-4(OCR/번역 슬롯 분리), M3-1·3-2(canvas/background 앵커와 안정적인 이미지 키), M4-3(그라데이션 허용치).
- **다음 단계**: M2-1·2의 Paddle 검출/인식 분리와 동적 CJK 방향 분류, M3-3·4의 빠른/중첩 스크롤 보정, M4-1·2의 연결 성분 말풍선 분석 및 M4-4·5 개발자 시각화·밴드 승격은 더 넓은 모델/오버레이 구조 변경이므로 별도 정확도 기준선과 실제 뷰어 검증을 거쳐 진행한다.

---

## M0 — 측정 기반 만들기

속도·정확도 주장을 숫자로 확인할 수 없으면 이후 마일스톤이 전부 추측이 된다. M1과 병행 가능하다.

**구현 방식**

- `local-server/scripts/bench_ocr.py` 추가. 고정 이미지 세트를 `translate_image` 내부 단계와 같은 순서로 돌리고, `main.py`가 이미 만드는 `ProcessingTimings`와 동일한 필드를 CSV로 출력한다. 번역 단계는 가짜 provider로 대체해 네트워크를 제외한다.
- 픽스처는 기존 `local-server/tests/fixtures/reader_two_bubbles_ocr.json` 옆에 익명화된 캡처 3~5장(밝은 말풍선 / 검은 내레이션 박스 / 세로쓰기 / 그라데이션 배경 / 화면 경계에 걸린 말풍선)을 둔다.
- 정확도 기준선은 각 픽스처에 기대 박스·기대 판정(`complete`/`clipped`/`absent`)을 손으로 붙인 JSON으로 둔다.

**완료 기준**: `python scripts/bench_ocr.py`가 단계별 ms와 말풍선 판정 정확도를 한 표로 출력하고, 두 번 연속 실행 시 총 시간 편차가 10% 이내.

---

## M1 — 번역 유실·중복 버그 (최우선)

### 1-1. 동일 텍스트·동일 크기 영역이 양쪽 다 삭제됨 — 확인됨

`local-server/app/ocr.py`의 `discard_contained_ocr_regions()`는 텍스트가 같을 때 `outer_area < inner_area`면 건너뛰지만, **면적이 정확히 같으면** 양쪽 모두 상대를 중복으로 판정한다. 결과적으로 두 영역이 다 버려지고 그 말풍선에는 번역이 아예 표시되지 않는다.

```
확인: 완전히 동일한 region 2개 입력 → 반환 0개
```

브라우저 쪽 쌍둥이 함수인 `extension/shared/regions.js`의 `discardContainedCandidates()`는 이미 결정적 승자 규칙을 갖고 있다:

```js
if (outerArea < candidateArea || (outerArea === candidateArea && outerIndex > index)) return false;
```

**구현 방식**: 같은 타이브레이커를 서버로 이식한다. 동률일 때 인덱스가 뒤인 쪽만 상대를 중복으로 보게 해서 정확히 하나가 살아남게 한다.

### 1-2. OCR 오인식 한 글자로 부분집합 판정이 실패함 — 확인됨

부분집합 억제가 **문자열 포함**을 필수 조건으로 요구하기 때문에, 같은 말풍선의 조각을 검출기가 `GONE` / `G0NE`처럼 한 글자 다르게 읽으면 포함 판정이 깨져 둘 다 남는다. 화면에는 패치가 겹쳐 뜨고, 번역 API 문자 수도 두 번 나간다.

```
확인: "I THOUGHT YOU WERE GONE" + "YOU WERE G0NE"(85% 이상 겹침) → 2개 유지
      "I THOUGHT YOU WERE GONE" + "YOU WERE GONE"            → 1개 유지
```

**구현 방식** — `ocr.py`의 `_normalized_region_text()`와 `_text_contains_fragment()` 주변에 두 단계를 더한다.

1. 비교 전용 정규화 추가: 구두점·공백 제거 + 혼동 문자 통일(`0↔O`, `1↔l↔I`, `5↔S`, `8↔B`, `2↔Z`). 표시용 텍스트는 건드리지 않고 비교 키만 만든다.
2. 그래도 포함이 성립하지 않을 때의 기하 우선 규칙: 작은 박스의 면적 겹침 비율이 `0.95` 이상이고 작은 쪽 면적이 큰 쪽의 `0.9` 이하이면 텍스트와 무관하게 중복으로 본다. 임계는 기존 `CONTAINED_REGION_OVERLAP_RATIO`(0.85)와 별도 상수로 둔다 — 인접한 두 말풍선을 잘못 지우지 않으려면 기하 단독 규칙이 더 엄격해야 한다.

두 단계 모두 `regions.js` 쪽에도 같은 값으로 이식한다. 이 함수 쌍은 이미 의도적으로 이중 방어이므로 한쪽만 고치면 안 된다.

### 1-3. 캡처 사이의 중복 (A ⊃ B가 서로 다른 캡처에서 도착)

`mergeRegions()`는 `discardContainedCandidates(incoming)`으로 **들어온 배치 안에서만** 포함 관계를 정리한다. 저장된 엔트리와는 비교하지 않는다. 밴드 오버랩(`DEFAULT_BAND_OVERLAP_PX = 130`) 구간에서 같은 말풍선이 이전 캡처와 다르게 그룹핑되면 — 예를 들어 1차에서 한 덩어리, 2차에서 두 줄로 — `findMatchingEntry()`가 하나만 기존 엔트리에 붙이고 나머지는 **새 엔트리**가 되어 같은 자리에 두 패치가 쌓인다.

**구현 방식**: `mergeRegions()`의 miss 정리 루프 바로 앞에 포함 스윕을 한 번 더 넣는다. 대상은 `coveredRect` 안에 완전히 들어오는 엔트리로 한정하고(이번 캡처가 실제로 본 범위만 판단), 승자는 `lastSeenAt`이 최신인 쪽, 동률이면 박스가 큰 쪽으로 한다. 반환값에 `collapsed` 카운트를 추가해 툴바 상세에 노출한다.

### 1-4. 처리 경합이 빨간 에러로 표시됨

`main.py`의 `processing_slot`이 잠겨 있으면 429 + `"다른 화면을 처리 중입니다"`가 나가고, `background.js`의 `requestJson()`은 이를 일반 예외로 던져 오버레이에 오류 상태로 뜬다. 같은 탭은 `this.inFlight`로 막히지만 워엄업·수동 번역·다중 탭에서는 발생한다.

**구현 방식**: `translateVisibleFrame()`에서 429를 `{ throttled: true, retryAfter }`로 변환한다. 이미 `MIN_CAPTURE_INTERVAL_MS` 스로틀에 대해 콘텐츠 스크립트가 그 형태를 처리하고 있으므로 새 처리 경로는 필요 없다.

**M1 완료 기준**

- `local-server/tests/test_ocr_grouping.py`에 1-1·1-2 회귀 케이스 추가 (동일 영역 2개 → 1개, 혼동 문자 조각 → 1개, 인접한 동일 대사 두 말풍선 → 2개 유지).
- `extension/tests/regions.merge.test.mjs`에 1-3 케이스 추가 (1차 캡처에서 한 덩어리, 2차에서 두 조각 → 최종 엔트리 1개).
- `node --test "extension/tests/**/*.test.mjs"` 71개 + 신규 전부 통과, `python -m unittest discover -s tests` 통과.

---

## M2 — 체감 속도 (안전한 최적화 범위)

### 2-1. 검출 우선 파이프라인 — 인식 비용을 말풍선 안쪽으로 제한

지금은 `PaddleOcrEngine.recognize()`가 `reader.predict()` 한 번으로 검출+인식을 모두 끝낸 뒤, `partition_regions()`가 말풍선 밖 글자를 버린다. 즉 **버릴 글자의 인식 비용을 이미 다 지불한 뒤에 버린다.** 웹툰 뷰어 화면에서는 UI 텍스트·효과음·간판이 검출 박스의 상당 비율을 차지한다.

설치된 `paddleocr 3.7.0`은 `TextDetection`과 `TextRecognition`을 공개 클래스로 노출한다(확인함: `paddleocr/__init__.py`).

**구현 방식** — `ocr.py`를 2단계로 나눈다.

1. `TextDetection`으로 박스만 받는다.
2. 각 박스에 대해 `bubble.analyze_bubble()`의 **`absent` 판정만** 먼저 적용한다(값싼 컷). `clipped` 판정은 이 단계에서 하지 않는다 — 잘림 여부는 그룹핑된 말풍선 단위로 봐야 정확하고, 좌표를 `main.py`의 `incompleteRegions` 응답에 그대로 실어야 하기 때문이다.
3. 살아남은 박스만 `TextRecognition`에 넘긴다.
4. 이후 `filter_regions_by_source_language` → `group_ocr_regions` → `discard_contained_ocr_regions` → 신뢰도 필터 → `partition_regions`(완전/잘림)는 지금 순서를 그대로 둔다.

`requireCompleteBubble`이 꺼져 있으면 2단계를 건너뛰고 기존 경로와 동일하게 동작해야 한다.

`ProcessingTimings`에 `detectionMs` / `recognitionMs` / `preRecognitionDroppedCount`를 추가하고, `main.py`의 한 줄 로그와 툴바 상세 문구에 반영한다.

### 2-2. CJK 텍스트라인 방향 분류를 조건부로

`_reader_for()`는 `use_textline_orientation = source_language not in FAST_HORIZONTAL_OCR_LANGUAGES`로 두어 **ko·ja·zh는 항상** 라인별 방향 분류 모델을 돌린다. 가로쓰기 일본어 웹툰에서는 순수 낭비다.

**구현 방식**: 언어별 리더 캐시 키를 `(paddle_language, orientation_on)` 쌍으로 바꾼다. 2-1의 검출 결과에서 세로형 박스 비율(`height / width > 1.5`)을 세고, 임계(초안 0.15) 미만이면 방향 분류 없는 리더로 인식한다. 판정은 캡처 단위로 하되 같은 `cacheScope` 안에서 최근 3회 판정을 유지해 스크롤마다 리더가 번갈아 잡히지 않게 한다.

### 2-3. CJK에도 검출 해상도 상한 적용

`text_det_limit_side_len = 1280`은 `FAST_HORIZONTAL_OCR_LANGUAGES`에만 걸려 있어, ko·ja·zh는 고DPI 캡처가 검출기에 원본 해상도로 들어간다. `background.js`가 이미 `OCR_RASTER_LONG_EDGE_BUDGET = 2560`까지만 줄여 보내므로 서버 쪽에서 한 번 더 걸러야 한다.

**구현 방식**: CJK용 상한 상수를 별도로 `1536`(세로쓰기 여유분)으로 두고 `text_det_limit_type: "max"`와 함께 적용한다. M0 정확도 기준선에서 세로쓰기 픽스처의 검출 누락이 늘지 않는지 확인한다.

### 2-4. 서버 처리 슬롯 분리

`main.py`의 `processing_slot = BoundedSemaphore(1)`이 요청 전체를 감싸므로, 클라우드 번역 왕복(`PROVIDER_REQUEST_TIMEOUT_SECONDS = 10`) 동안 다음 캡처의 OCR이 시작조차 못 한다. 스크롤을 이어가는 동안 이게 그대로 대기 시간이 된다.

**구현 방식**: 슬롯을 둘로 나눈다. OCR 구간은 기존대로 1개(모델 상태와 메모리 보호 — `PaddleOcrEngine._inference_lock`과 목적이 같다), 번역 구간은 별도 세마포어 2~3개. 요청은 OCR 슬롯을 잡고 인식을 끝낸 뒤 **놓고** 번역 슬롯을 잡는다. 429는 OCR 슬롯이 잠겼을 때만 나간다.

### 2-5. MKLDNN 재시도

`enable_mkldnn: False`는 Paddle 3.3 new-IR 경로의 실패 때문에 명시적으로 꺼둔 것이다. 설치된 버전은 `paddlepaddle 3.3.1`이므로 그대로 두되, 자동 판정으로 바꾼다.

**구현 방식**: 리더 생성 시 MKLDNN을 켜서 만들고 즉시 워엄업 추론(현재 `warmup()`이 쓰는 64×64 이미지)을 돌린다. 예외가 나면 그 리더를 버리고 MKLDNN 없이 다시 만든 뒤 결과를 프로세스 수명 동안 기억한다. `/health`에 `mkldnn` 필드로 실제 사용 여부를 노출한다.

### 2-6. base64 업로드 제거 (선택)

`/v1/translate-image`가 이미지를 data URL 문자열로 받아 페이로드가 약 33% 커지고, 서버에서 `base64.b64decode` 비용이 더해진다. `background.js`의 `blobToDataUrl()`도 전체 바이트를 문자열로 조립한다.

**구현 방식**: `multipart/form-data` 엔드포인트를 추가하고 JSON 경로는 호환용으로 유지한다. M0 벤치에서 `decodeMs` + 왕복 시간 감소가 유의미할 때만 채택한다.

**M2 완료 기준**

- M0 벤치 기준 총 처리 시간이 대표 픽스처에서 **30% 이상** 감소.
- 말풍선 판정 정확도가 M0 기준선 대비 하락 없음.
- `local-server/tests/test_ocr_adapter.py`에 검출-우선 경로 테스트 추가: 말풍선 밖 박스가 인식 단계에 넘어가지 않는지를 가짜 인식기 호출 횟수로 검증.

---

## M3 — 스크롤 중 위치 안정성

### 3-1. canvas·배경이미지 뷰어에 앵커가 없음

`anchorForDocumentRect()`는 `imageAtPoint()`가 돌려주는 `HTMLImageElement`만 앵커로 받는다. 같은 파일의 `autoComicCaptureRegion()`은 `img, canvas`를 모두 훑는데도 그렇다. canvas로 그리거나 `background-image`로 패널을 까는 뷰어에서는 모든 패치가 문서 앵커로 떨어지고, 지연 로딩으로 페이지가 밀리면 전부 어긋난다.

**구현 방식**: `imageAtPoint()`를 `anchorTargetAtPoint()`로 넓힌다. 우선순위는 `<img>` → `<canvas>` → 계산된 `background-image`가 있고 면적이 뷰포트의 10% 이상인 블록 요소. 키 생성은 `anchorKeyForImage()`를 `anchorKeyFor(element)`로 일반화하되, `src`가 없는 요소는 `tagName + DOM 경로 + 형제 인덱스`를 키로 쓴다. `elementRectsForKeys()`의 재탐색 경로(`document.images` 한 번 순회)도 같은 규칙으로 확장해야 하며, 여기서 전체 DOM을 매번 훑지 않도록 키에 태그명을 넣어 후보를 좁힌다.

### 3-2. `currentSrc` 기반 앵커 키가 responsive 이미지에서 깨짐

`imageSource()`는 `currentSrc`를 우선 쓴다. 창 크기나 DPR이 바뀌어 브라우저가 `srcset`을 다시 고르면 같은 요소의 키가 바뀌고, 저장된 엔트리의 앵커는 해결 불가가 되어 `sync()`가 그 패치를 그리지 않는다(`resolveAnchorRect()`가 `null`을 돌려주는 것은 의도된 동작이다).

**구현 방식**: 키 생성 시 `getAttribute("src")`를 우선하고 없을 때만 `currentSrc`로 폴백한다. 추가로 `rememberAnchorElement()`가 요소에 1회성 심볼 프로퍼티를 심어, 키 문자열이 바뀌어도 같은 요소를 되찾을 수 있게 한다.

### 3-3. 스크롤 프레임 지연

`.overlay-shell`은 `position: fixed`이고 `OverlayView.sync()`가 패치마다 `translate3d`를 다시 계산한다. 스크롤은 컴포지터에서 진행되고 재배치는 JS `requestAnimationFrame`에서 일어나므로, 빠른 스크롤에서 패치가 한 프레임씩 뒤처져 “헤엄치는” 느낌이 난다.

**구현 방식(보수적)**: `.regions` 컨테이너 하나만 스크롤 델타로 옮기고, 개별 패치의 transform은 건드리지 않는다.

- 각 패치를 컨테이너 기준 좌표로 한 번만 배치한다.
- 스크롤 이벤트에서는 `regions.style.transform = translate3d(-Δx, -Δy, 0)` 하나만 갱신한다.
- 레이아웃이 실제로 바뀐 프레임(리사이즈, 이미지 로드, 중첩 스크롤, 새 스캔 결과)에서만 전체 `sync()`를 돌린다.

패치별 히트 테스트용 `dataset.hit*` 값도 같은 기준으로 바뀌므로 `updateHover()`에서 컨테이너 오프셋을 더해 읽어야 한다.

문서 좌표 `position: absolute`로 완전히 옮기는 안은 **보류**한다(아래 보류 목록 참조).

### 3-4. 중첩 스크롤러에서 문서 앵커 패치가 사라짐

`onViewportChange()`가 루트가 아닌 스크롤을 감지하면 `documentAnchorsStale = true`로 두고, `syncOverlays()`는 요소 앵커 엔트리만 그린다. 이 플래그는 `scan()`이 성공적으로 merge를 마쳐야 해제된다. 즉 중첩 스크롤러 뷰어에서는 스크롤할 때마다 문서 앵커 번역이 통째로 사라졌다가 다음 스캔에서 돌아온다.

**구현 방식**: 스크롤을 일으킨 스크롤러 요소를 앵커 대상으로 등록하고, 그 요소의 `scrollTop`/`scrollLeft` 변화량으로 문서 앵커 rect를 보정한다. 3-1의 앵커 일반화 위에 얹는다.

**M3 완료 기준**

- `extension/tests/regions.geometry.test.mjs`에 앵커 키 안정성 케이스 추가(`srcset` 변경 시나리오, canvas 앵커 해결).
- 실제 뷰어 두 곳(`<img>` 기반 / canvas 기반)에서 빠른 스크롤 중 패치 뒤처짐이 눈에 띄지 않고, 되돌아왔을 때 패치가 사라지지 않음.

---

## M4 — 말풍선 인식 정확도

### 4-1. 축 방향 라인 스캔을 연결 성분 방식으로 교체

`bubble.py`의 `_expand()`는 텍스트 박스의 네 변에서 바깥쪽으로 행/열 평균을 훑으며 `FILL_LINE_RATIO = 0.7` 미만이 `BOUNDARY_STREAK_PX = 2`번 연속되면 경계로 본다. 이 방식은 꼬리(tail)가 달린 말풍선, 기울어진 말풍선, 서로 겹친 말풍선, 둥근 모서리에서 경계를 잘못 잡는다.

**구현 방식**: 다운스케일한 fill 마스크(예: 긴 변 512px) 위에서 텍스트 박스 중심부터 BFS 연결 성분을 구한다.

- 성분이 이미지 경계에 닿으면 → `clipped`, 닿은 변을 `edges`로.
- 성분 면적이 텍스트 박스 면적의 일정 배수 미만이면 → `absent`(말풍선이 아니라 글자 배경).
- 성분의 볼록껍질 대비 면적비(솔리디티)가 낮으면 → 말풍선이 아닌 배경으로 본다.

`analyze_bubble()`의 반환 타입(`BubbleAnalysis`)과 `partition_regions()`의 계약은 그대로 두어 `main.py`는 변경하지 않는다.

### 4-2. `MAX_EXPAND_PX = 260` 상한이 큰 말풍선을 잘못 통과시킴

탐색 예산을 다 쓰면 `_expand()`가 `"open"`을 돌려주고, `analyze_bubble()`은 `edges`가 비었으므로 `complete`로 판정한다. 즉 **텍스트에서 260px 넘게 이어지는 큰 말풍선은 실제로 화면 밖으로 잘려 있어도 완전한 것으로 취급되어 반쪽 문장이 번역된다.** 4-1의 연결 성분 방식으로 바꾸면 예산 상한 자체가 사라진다(비용은 다운스케일로 통제).

### 4-3. 그라데이션·스크린톤 말풍선이 조용히 버려짐

`fill = |gray - fill_value| <= FILL_TOLERANCE`의 `FILL_TOLERANCE = 46`은 고정값이다. 그라데이션 배경 말풍선은 위아래 밝기 차가 이보다 커서 마스크가 끊기고, `fill_kind = "mid"` 경로에서 `blocked`가 하나도 안 나오면 `absent`로 조용히 버려진다.

**구현 방식**: tolerance를 텍스트 박스 주변 링의 밝기 표준편차에 비례시킨다(`max(FILL_TOLERANCE, k * std)`, 상한을 둔다). 그라데이션 픽스처가 M0 기준선에서 `complete`로 판정되는지로 검증한다.

### 4-4. 버려진 영역을 볼 수 없음

지금은 툴바 상세에 `말풍선 밖 글자 N개 제외`라는 **개수만** 뜬다. 판정이 틀렸을 때 사용자도 개발자도 무엇이 버려졌는지 알 수 없다.

**구현 방식**: 응답에 `absentRegions`(좌표만, 텍스트 없이)를 추가하고, 설정에 개발자용 토글을 둬서 켰을 때만 점선 박스로 표시한다. 텍스트를 응답에 싣지 않으므로 로그 정책은 그대로 유지된다.

### 4-5. 밴드 스캔과 완전 말풍선 조건의 교착 확인

밴드+오버랩(`DEFAULT_BAND_OVERLAP_PX = 130`)보다 큰 말풍선은 매번 `clipped`로 판정되고, `trimBandForClipped()`가 그 구간을 커버리지에서 빼므로 같은 자리를 계속 다시 읽는다. `MAX_CONSECUTIVE_BAND_SCANS = 8`에 걸려 전체 스캔으로 넘어가야 겨우 풀린다.

**구현 방식**: 먼저 `extension/tests/regions.band.test.mjs`에 이 시나리오를 회귀 테스트로 고정한다. 실제로 8회를 낭비하는 것이 확인되면, 같은 밴드에서 같은 변이 연속으로 잘렸을 때 즉시 전체 스캔으로 승격하는 규칙을 `incrementalBand()`에 추가한다.

**M4 완료 기준**

- M0 정확도 기준선에서 `clipped`/`absent`/`complete` 판정이 픽스처 전체에 대해 기대값과 일치.
- `local-server/tests/test_bubble_detection.py`에 4-1·4-2·4-3 케이스 추가.
- 4-5 회귀 테스트 통과.

---

## 보류 (지금은 하지 않음, 이유를 남김)

| 항목 | 이유 |
| --- | --- |
| ONNXRuntime 직접 실행으로 교체 | 속도 폭은 가장 크지만 Paddle의 전처리·후처리를 직접 이식해야 하고 정확도 회귀 검증 부담이 크다. M2 결과가 부족할 때 재검토. |
| GPU 가속 | 대상 PC의 CUDA 가능 GPU 유무가 전제. 확인되면 별도 마일스톤. |
| 오버레이를 문서 좌표 `position: absolute`로 전환 | 스크롤 추적 JS를 완전히 없앨 수 있으나, `transform`이 걸린 조상·`overflow` 컨테이너·`position: sticky` 헤더가 있는 뷰어에서 좌표계가 깨진다. M3-3의 컨테이너 단일 이동으로 대부분의 체감 문제가 해소되는지 먼저 본다. |
| 번역 캐시를 디스크에 영속화 | 현재 캐시는 의도적으로 인메모리·회차 한정이다(`TranslationService._activate_cache_scope`). 회차 간 재사용은 개인정보 판단이 따로 필요하다. |

---

## 전체 검증

각 마일스톤이 끝날 때마다:

```powershell
# 서버
cd local-server
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\bench_ocr.py        # M0 이후

# 확장 프로그램
node --test "extension/tests/**/*.test.mjs"
```

실제 동작 확인:

1. 로컬 서버를 띄우고 `chrome://extensions`에서 확장 새로고침 → 웹툰 탭 새로고침.
2. 설정의 **연결 확인**에서 확장·서버 버전이 같은지 확인.
3. 툴바 상태 문구에 마우스를 올려 단계별 시간(M2 이후 `detectionMs`/`recognitionMs` 포함)을 읽는다.
4. 회귀 체크리스트: 같은 말풍선에 패치가 두 겹으로 뜨지 않는가 / 화면 경계에 걸린 말풍선이 스크롤 후 번역되는가 / 되돌아간 구간이 서버 호출 없이 즉시 복원되는가 / 빠른 스크롤에서 패치가 따라오는가.

버전 표기는 `manifest.json`, `local-server/app/main.py`의 `APP_VERSION`, `package.json` 세 곳을 함께 올린다.
