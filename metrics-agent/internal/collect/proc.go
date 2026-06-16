package collect

import (
	"bufio"
	"io/fs"
	"strconv"
	"strings"
)

type Proc struct {
	Root fs.FS
}

type CPUStat struct {
	Total int64
	Idle  int64
}

type MemStat struct {
	TotalBytes int64
	UsedBytes  int64
}

func (p *Proc) read(name string) ([]string, error) {
	f, err := p.Root.Open(name)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var lines []string
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1024*1024), 1024*1024)
	for sc.Scan() {
		lines = append(lines, sc.Text())
	}
	return lines, sc.Err()
}

func (p *Proc) CPU() (CPUStat, error) {
	lines, err := p.read("stat")
	if err != nil {
		return CPUStat{}, err
	}
	for _, l := range lines {
		if !strings.HasPrefix(l, "cpu ") {
			continue
		}
		f := strings.Fields(l)[1:]
		var total int64
		var idle int64
		for i, s := range f {
			v, _ := strconv.ParseInt(s, 10, 64)
			total += v
			if i == 3 {
				idle = v
			}
		}
		return CPUStat{Total: total, Idle: idle}, nil
	}
	return CPUStat{}, nil
}

func (p *Proc) Mem() (MemStat, error) {
	lines, err := p.read("meminfo")
	if err != nil {
		return MemStat{}, err
	}
	kv := map[string]int64{}
	for _, l := range lines {
		f := strings.Fields(l)
		if len(f) < 2 {
			continue
		}
		key := strings.TrimSuffix(f[0], ":")
		v, _ := strconv.ParseInt(f[1], 10, 64)
		kv[key] = v * 1024
	}
	total := kv["MemTotal"]
	return MemStat{TotalBytes: total, UsedBytes: total - kv["MemAvailable"]}, nil
}

func (p *Proc) NetTotals() (rx, tx int64, err error) {
	lines, err := p.read("net/dev")
	if err != nil {
		return 0, 0, err
	}
	for _, l := range lines {
		if !strings.Contains(l, ":") {
			continue
		}
		parts := strings.SplitN(l, ":", 2)
		iface := strings.TrimSpace(parts[0])
		if iface == "lo" {
			continue
		}
		f := strings.Fields(parts[1])
		if len(f) < 9 {
			continue
		}
		r, _ := strconv.ParseInt(f[0], 10, 64)
		w, _ := strconv.ParseInt(f[8], 10, 64)
		rx += r
		tx += w
	}
	return rx, tx, nil
}

func allDigits(s string) bool {
	if len(s) == 0 {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

func isPartition(name string, all []string) bool {
	for _, b := range all {
		if b == name || !strings.HasPrefix(name, b) {
			continue
		}
		rest := name[len(b):]
		if strings.HasPrefix(rest, "p") && allDigits(rest[1:]) {
			return true
		}
		last := b[len(b)-1]
		if (last < '0' || last > '9') && allDigits(rest) {
			return true
		}
	}
	return false
}

func (p *Proc) DiskIOBytes() (read, written int64, err error) {
	lines, err := p.read("diskstats")
	if err != nil {
		return 0, 0, err
	}
	type diskLine struct {
		name string
		rd   int64
		wr   int64
	}
	var parsed []diskLine
	var names []string
	for _, l := range lines {
		f := strings.Fields(l)
		if len(f) < 10 {
			continue
		}
		rd, _ := strconv.ParseInt(f[5], 10, 64)
		wr, _ := strconv.ParseInt(f[9], 10, 64)
		parsed = append(parsed, diskLine{name: f[2], rd: rd, wr: wr})
		names = append(names, f[2])
	}
	for _, d := range parsed {
		if isPartition(d.name, names) {
			continue
		}
		read += d.rd * 512
		written += d.wr * 512
	}
	return read, written, nil
}
