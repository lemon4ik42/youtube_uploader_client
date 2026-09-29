from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QSpinBox, QWidget

WEEK_SECONDS = 7 * 24 * 3600

# (суффикс, множитель в секундах, максимум значения в этих единицах)
_UNITS = [
    ("sec", 1, WEEK_SECONDS),
    ("min", 60, WEEK_SECONDS // 60),
    ("hours", 3600, WEEK_SECONDS // 3600),
    ("days", 86400, WEEK_SECONDS // 86400),
]


class DelayInputWidget(QWidget):
    """Ввод задержки с выбором единиц (сек/мин/часы/дни).

    Внутри хранит полное значение в секундах; при смене единиц значение
    конвертируется без потери точности, а максимальное ограничение —
    одна неделя (серверное ограничение inter_upload_delay_seconds).
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QHBoxLayout

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._value = QSpinBox()
        self._unit = QComboBox()
        for suffix, _mult, _maxv in _UNITS:
            self._unit.addItem(suffix)
        self._unit.setCurrentIndex(0)
        self._apply_unit_limits()
        self._unit.currentIndexChanged.connect(self._on_unit_changed)
        layout.addWidget(self._value)
        layout.addWidget(self._unit)
        layout.addStretch()

    def _apply_unit_limits(self) -> None:
        index = self._unit.currentIndex()
        _suffix, _mult, max_value = _UNITS[index]
        # Держим максимум кратным множителю, чтобы перевод туда-обратно
        # не терял секунды (неделя кратна всем единицам, но на будущее).
        self._value.setRange(0, max_value)

    def _on_unit_changed(self, _index: int) -> None:
        # Значение уже в секундах — конвертируем его в новые единицы напрямую.
        # Не вызываем set_seconds: он сам выбирает «удобные» единицы и затёр бы
        # только что выбранные пользователем.
        seconds = self._seconds_cache if hasattr(self, "_seconds_cache") else self.seconds()
        self._apply_unit_limits()
        mult = _UNITS[self._unit.currentIndex()][1]
        # Округляем вверх: дробное значение (например, 3600s в днях) не должно
        # превращаться в 0 и терять введённое время.
        self._value.setValue(-(-seconds // mult))

    def seconds(self) -> int:
        mult = _UNITS[self._unit.currentIndex()][1]
        self._seconds_cache = self._value.value() * mult
        return self._seconds_cache

    def set_seconds(self, seconds: int) -> None:
        seconds = max(0, min(WEEK_SECONDS, int(seconds)))
        self._seconds_cache = seconds
        # Выбираем самые крупные единицы, в которых значение делится без
        # остатка, чтобы не показывать "90 minutes" вместо "1 hours 30 min".
        for index in range(len(_UNITS) - 1, -1, -1):
            _suffix, mult, _maxv = _UNITS[index]
            if mult > 1 and seconds % mult == 0 and seconds // mult <= _UNITS[index][2]:
                self._unit.setCurrentIndex(index)
                self._apply_unit_limits()
                self._value.setValue(seconds // mult)
                return
        # Иначе — секунды.
        self._unit.setCurrentIndex(0)
        self._apply_unit_limits()
        self._value.setValue(seconds)
