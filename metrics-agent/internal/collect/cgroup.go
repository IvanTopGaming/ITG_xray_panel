package collect

import (
	"bufio"
	"io/fs"
	"strconv"
	"strings"
)

type Cgroup struct {
	Root fs.FS
}

type CtrStat struct {
	CPUUsec  int64
	MemBytes int64
	IOBytes  int64
}

func (c *Cgroup) Containers() ([]string, error) {
	var ids []string
	if entries, err := fs.ReadDir(c.Root, "system.slice"); err == nil {
		for _, e := range entries {
			n := e.Name()
			if strings.HasPrefix(n, "docker-") && strings.HasSuffix(n, ".scope") {
				ids = append(ids, strings.TrimSuffix(strings.TrimPrefix(n, "docker-"), ".scope"))
			}
		}
	}
	if entries, err := fs.ReadDir(c.Root, "docker"); err == nil {
		for _, e := range entries {
			if e.IsDir() && isHexID(e.Name()) {
				ids = append(ids, e.Name())
			}
		}
	}
	return ids, nil
}

func isHexID(s string) bool {
	if len(s) < 8 {
		return false
	}
	for _, r := range s {
		if !((r >= '0' && r <= '9') || (r >= 'a' && r <= 'f')) {
			return false
		}
	}
	return true
}

func (c *Cgroup) dir(id string) string {
	scope := "system.slice/docker-" + id + ".scope"
	if _, err := fs.Stat(c.Root, scope); err == nil {
		return scope
	}
	return "docker/" + id
}

func (c *Cgroup) Stats(id string) (CtrStat, error) {
	d := c.dir(id)
	var st CtrStat
	st.CPUUsec = c.keyed(d+"/cpu.stat", "usage_usec")
	st.MemBytes = c.single(d + "/memory.current")
	st.IOBytes = c.ioBytes(d + "/io.stat")
	return st, nil
}

func (c *Cgroup) single(name string) int64 {
	f, err := c.Root.Open(name)
	if err != nil {
		return 0
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	if sc.Scan() {
		v, _ := strconv.ParseInt(strings.TrimSpace(sc.Text()), 10, 64)
		return v
	}
	return 0
}

func (c *Cgroup) keyed(name, key string) int64 {
	f, err := c.Root.Open(name)
	if err != nil {
		return 0
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		f := strings.Fields(sc.Text())
		if len(f) == 2 && f[0] == key {
			v, _ := strconv.ParseInt(f[1], 10, 64)
			return v
		}
	}
	return 0
}

func (c *Cgroup) ioBytes(name string) int64 {
	f, err := c.Root.Open(name)
	if err != nil {
		return 0
	}
	defer f.Close()
	var total int64
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		for _, tok := range strings.Fields(sc.Text()) {
			if strings.HasPrefix(tok, "rbytes=") || strings.HasPrefix(tok, "wbytes=") {
				v, _ := strconv.ParseInt(tok[strings.IndexByte(tok, '=')+1:], 10, 64)
				total += v
			}
		}
	}
	return total
}
