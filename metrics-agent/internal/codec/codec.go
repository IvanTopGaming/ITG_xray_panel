package codec

import (
	"bytes"
	"encoding/binary"
	"fmt"

	"github.com/itg/metrics-agent/internal/model"
	"github.com/klauspost/compress/zstd"
)

const Codec = 1

var (
	enc, _ = zstd.NewWriter(nil)
	dec, _ = zstd.NewReader(nil)
)

func Encode(points []model.Point) ([]byte, error) {
	var raw bytes.Buffer
	tmp := make([]byte, binary.MaxVarintLen64)

	var prevTs, prevDelta int64
	for _, p := range points {
		delta := p.Ts - prevTs
		dod := delta - prevDelta
		raw.Write(tmp[:binary.PutVarint(tmp, dod)])
		prevTs = p.Ts
		prevDelta = delta
	}
	var prevVal int64
	for _, p := range points {
		raw.Write(tmp[:binary.PutVarint(tmp, p.Val-prevVal)])
		prevVal = p.Val
	}
	return enc.EncodeAll(raw.Bytes(), nil), nil
}

func Decode(blob []byte, n int) ([]model.Point, error) {
	if n == 0 {
		return nil, nil
	}
	raw, err := dec.DecodeAll(blob, nil)
	if err != nil {
		return nil, err
	}
	r := bytes.NewReader(raw)
	out := make([]model.Point, n)

	var prevTs, prevDelta int64
	for i := 0; i < n; i++ {
		dod, err := binary.ReadVarint(r)
		if err != nil {
			return nil, fmt.Errorf("ts varint %d: %w", i, err)
		}
		delta := prevDelta + dod
		prevTs += delta
		prevDelta = delta
		out[i].Ts = prevTs
	}
	var prevVal int64
	for i := 0; i < n; i++ {
		d, err := binary.ReadVarint(r)
		if err != nil {
			return nil, fmt.Errorf("val varint %d: %w", i, err)
		}
		prevVal += d
		out[i].Val = prevVal
	}
	if r.Len() != 0 {
		return nil, fmt.Errorf("codec: %d trailing bytes, n=%d mismatch", r.Len(), n)
	}
	return out, nil
}
