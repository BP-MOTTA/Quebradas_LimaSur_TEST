from shapely.geometry import Polygon

from quebradas.acquisition.coverage import coverage_fraction

AOI = Polygon([(0, 0), (0.01, 0), (0.01, 0.01), (0, 0.01), (0, 0)])


def test_full_coverage() -> None:
    scene = Polygon([(-0.001, -0.001), (0.011, -0.001), (0.011, 0.011), (-0.001, 0.011)])

    assert coverage_fraction(scene, AOI) == 1.0


def test_partial_coverage() -> None:
    scene = Polygon([(0, 0), (0.005, 0), (0.005, 0.01), (0, 0.01)])

    assert 0.49 < coverage_fraction(scene, AOI) < 0.51


def test_outside_coverage() -> None:
    scene = Polygon([(0.02, 0.02), (0.03, 0.02), (0.03, 0.03), (0.02, 0.03)])

    assert coverage_fraction(scene, AOI) == 0.0
