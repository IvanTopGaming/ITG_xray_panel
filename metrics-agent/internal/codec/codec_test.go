package codec

import (
	"math/rand"
	"testing"

	"github.com/itg/metrics-agent/internal/model"
)

func TestDecodeRejectsShortN(t *testing.T) {
	in := []model.Point{{Ts: 1, Val: 1}, {Ts: 2, Val: 2}, {Ts: 3, Val: 3}}
	b, _ := Encode(in)
	if _, err := Decode(b, 2); err == nil {
		t.Fatal("expected error decoding with short n, got nil")
	}
}

func TestRoundTripEmpty(t *testing.T) {
	b, err := Encode(nil)
	if err != nil {
		t.Fatal(err)
	}
	got, err := Decode(b, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 0 {
		t.Fatalf("want 0 points, got %d", len(got))
	}
}

func TestRoundTripRegular(t *testing.T) {
	var in []model.Point
	ts := int64(1_700_000_000)
	val := int64(0)
	for i := 0; i < 3600; i++ {
		ts += 1
		val += int64(rand.Intn(2000) - 1000)
		in = append(in, model.Point{Ts: ts, Val: val})
	}
	b, err := Encode(in)
	if err != nil {
		t.Fatal(err)
	}
	out, err := Decode(b, len(in))
	if err != nil {
		t.Fatal(err)
	}
	if len(out) != len(in) {
		t.Fatalf("len %d != %d", len(out), len(in))
	}
	for i := range in {
		if out[i] != in[i] {
			t.Fatalf("point %d: %+v != %+v", i, out[i], in[i])
		}
	}
}

func TestCompressesRegularSeries(t *testing.T) {
	var in []model.Point
	ts := int64(1_700_000_000)
	for i := 0; i < 3600; i++ {
		ts++
		in = append(in, model.Point{Ts: ts, Val: 1000})
	}
	b, _ := Encode(in)
	if len(b) >= len(in)*4 {
		t.Fatalf("expected strong compression, got %d bytes for %d points", len(b), len(in))
	}
}
